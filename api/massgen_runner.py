"""MassGen 简历评审调用层。

通过 subprocess 调用 `uv run massgen --automation`，等待编排结束后从
.massgen/massgen_logs/<run_id>/ 下解析：
  - 每个 agent 最新一轮的 answer.txt（平铺三份评审，不取 winner-only）
  - 每个 agent 最新一张 vote.json（投票对象与理由）
  - stdout 中的 WINNER 行
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "massgen" / "configs" / "resume_review.yaml"

# agent_id -> 维度中文名（顺序即返回顺序）
AGENT_DIMENSIONS: dict[str, str] = {
    "tech_reviewer": "技术",
    "experience_reviewer": "经历",
    "improvement_reviewer": "改进建议",
}

NOTE = "improvement_reviewer 的量化建议为示例模板，需候选人填真实数据"

# ---------------------------------------------------------------------------
# 答案清洗
#
# agent 输出有两类格式漂移：
#   1) 旧格式：正文在前，内部元信息在尾部
#   2) 新格式：先写一段“交付汇报头”（工作区/文件列表），再用显式围栏标注正文
#      起点（如“评审正文（…≤300字）：”“—— 评审正文 ——”），正文之后再挂
#      评审说明/Vote 评估/决策记录等区块
# 因此清洗策略是“围栏优先”：先找正文起点围栏，围栏外的头部元信息整体丢弃；
# 扫描时元信息行只跳过本行（可能出现在正文头部，不能全局截断），只有段落级
# 内部区块标题才截断其后整块。
# ---------------------------------------------------------------------------

# 段落级内部区块标题（归一化后 startswith）：命中后其后整块都是内部内容 → 截断
_INTERNAL_SECTION_HEADINGS = (
    "### 协调与决策记录",
    "协调与决策记录",
    "内部评审记录：",
    "内部评审记录:",
    "交付说明",
    "交付与验证说明",
    "验证说明",
    "决策日志",
    "决策记录",
    "评审说明",
    "Change Document",
    "Deliberation Trail",
    "Key Output Changes from Prior",
    "Open Gaps",
    "与既有答案的差异",
    "构建依据",
    "核查说明",
    "说明：本任务",
    "说明：本回答",
)

# 单行内部元信息（归一化后 startswith）：只跳过本行，绝不全局截断
_INTERNAL_LINE_STARTS = (
    "工作目录：",
    "工作目录:",
    "工作区：",
    "工作区:",
    "交付物：",
    "交付物:",
    "产出文件",
    "Task Status:",
    "Vote 评估：",
    "Vote 评估:",
    "决策要点",
    "我完成了",
)

# 行内包含即“跳过本行”的内部 token
_INTERNAL_LINE_CONTAINS = (
    "changedoc.md",
    "deliverable/",
    ".massgen_scratch",
    "未使用搜索/代码执行工具",
    "DEC-0",
    "为纯文本评审",
    "为纯文本教练",
    "无需文件交付物",
    "已按身份要求",
    "MCP 不可用",
)

# 正文起点围栏（作用于归一化后的行）：
#   评审正文（技术维度，≤300字）： / 评审正文： / —— 评审正文 ——
#   内容（约150字，符合≤300字要求）：
#   评审（≤300字，按优先级，每条一句话）：
_BODY_START_PATTERNS = (
    re.compile(r"^评审正文\s*(?:[（(].*)?[:：]?\s*$"),
    re.compile(r"^(?:内容|正文)\s*[（(][^）)]*字[^）)]*[）)]\s*[:：]?\s*$"),
    re.compile(r"^评审\s*[（(][^）)]*字[^）)]*[）)].*?[:：]?\s*$"),
)

# 字数自检行：（全文 297 字…）/（正文约 284 字…）/ 字数：276 / 上限 300
_SELF_CHECK_PATTERN = re.compile(r"[（(]\s*(?:全文|正文)?约?\s*\d+\s*字|^字数[:：]\s*\d")

# deliberation 行：引用其他 agent 编号（agent1.1 等）并带取舍/依据措辞
_AGENT_REF_PATTERN = re.compile(r"agent\d+\.\d+")
_DELIBERATION_WORD_PATTERN = re.compile(
    r"依据|采纳|未采纳|取舍|融合|剔除|extended|framing|skeleton|based on|Origin|Synthesis",
    flags=re.IGNORECASE,
)

# 匹配标记前剥掉的行首装饰：列表符号、加粗、引号、中英文破折号等
_LINE_PREFIX_CHARS = " -*#>•·\t—–"


def _normalize_line(line: str) -> str:
    s = line.strip()
    # 剥多层行首/行尾符号与加粗标记，如 "- **工作目录：" / "**说明**："
    # / "—— 评审正文 ——" → "工作目录：" / "说明：" / "评审正文"
    changed = True
    while changed:
        changed = False
        stripped = s.lstrip(_LINE_PREFIX_CHARS).strip().strip("*").strip()
        stripped = stripped.replace("*", "").strip()
        stripped = stripped.rstrip(_LINE_PREFIX_CHARS).strip()
        if stripped != s:
            s = stripped
            changed = True
    return s

_SUBPROCESS_TIMEOUT_SECONDS = 2400  # 40 分钟安全上限（orchestrator 自身超时 1800s）


def _run_command_sync(cmd: list[str], cwd: str, timeout: float) -> tuple[str, int]:
    """在工作线程中同步执行子进程，返回 (stdout 文本, 退出码)。

    必须用同步 subprocess 而非 asyncio.create_subprocess_exec：uvicorn 0.36+
    在 Windows 上以 --reload / 多 workers 启动时强制使用 SelectorEventLoop
    （Config.use_subprocess=True），而 Windows 的 SelectorEventLoop 不支持
    子进程传输，asyncio 子进程 API 会抛裸 NotImplementedError 导致任务秒失败。
    同步 subprocess 直接走 CreateProcess，与事件循环类型无关。
    """
    # 中文 Windows 子进程默认 cp936，massgen print emoji（🟡/❌）会
    # UnicodeEncodeError 崩在配置校验阶段。显式锁定 UTF-8，不依赖宿主终端代码页。
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("massgen 评审执行超时（>40 分钟），已终止子进程") from exc
    return proc.stdout.decode("utf-8", errors="replace"), proc.returncode


async def _exec_command(cmd: list[str], cwd: str, timeout: float) -> tuple[str, int]:
    """异步包装：把同步子进程执行丢到线程池，不阻塞事件循环。"""
    return await asyncio.to_thread(_run_command_sync, cmd, cwd, timeout)


def _massgen_launch_prefix() -> list[str]:
    """子进程入口前缀。

    优先直调当前解释器同目录的 massgen console script（venv/Scripts/massgen.exe
    或 venv/bin/massgen），绕过 `uv run` 每次的环境解析（实测热缓存省 ~15s/次）。
    console script 不存在时（非 venv 安装等）回退 `uv run massgen`，保证可用。
    """
    entry_name = "massgen.exe" if os.name == "nt" else "massgen"
    console_script = Path(sys.executable).resolve().parent / entry_name
    if console_script.exists():
        return [str(console_script)]
    return [_uv_executable(), "run", "massgen"]


def _build_command(prompt: str) -> list[str]:
    """构造 massgen 子进程命令（list 形式，不经 shell，参数不会被拆词）。"""
    return [
        *_massgen_launch_prefix(),
        "--automation",
        # 简历正文是纯数据，不是工作指令：关闭 @文件引用解析。
        # 否则简历里的 mAP@0.5、内部邮箱等 token 会被当路径，
        # 报 "Context paths not found" 并在编排开始前退出。
        "--no-parse-at-references",
        "--config",
        str(CONFIG_PATH.relative_to(PROJECT_ROOT)),
        prompt,
    ]


def _failure_reason_from_metadata(metadata_text: str) -> str:
    """从 execution_metadata.yaml 提取 massgen 记录的失败阶段与原因。

    子进程 configuration_error 时没有 Python traceback（只有 print 的错误行），
    但失败信息会落进 cli_args.failure_stage / failure_error。解析失败返回空串。
    """
    if not metadata_text.strip():
        return ""
    try:
        import yaml  # 延迟导入：massgen 本身依赖 PyYAML，环境里必然有

        data = yaml.safe_load(metadata_text) or {}
        cli_args = data.get("cli_args") or {}
        stage = cli_args.get("failure_stage")
        error = cli_args.get("failure_error")
    except Exception:
        return ""
    if not stage and not error:
        return ""
    return f"{stage or 'unknown_stage'}: {error}".strip().rstrip(":")


def _read_failure_reason(run_dir: Path) -> str:
    metadata = run_dir / "turn_1" / "attempt_1" / "execution_metadata.yaml"
    if not metadata.exists():
        return ""
    try:
        return _failure_reason_from_metadata(
            metadata.read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        return ""


def _uv_executable() -> str:
    uv = os.environ.get("UV")
    if uv:
        return uv
    candidate = Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "uv.exe"
    if candidate.exists():
        return str(candidate)
    return "uv"


def build_prompt(resume: str, jd: str | None) -> str:
    # 入口层已 strip 过，这里再 strip 一次兜底，两处判断逻辑一致
    jd = (jd or "").strip()
    if jd:
        return f"请评审以下简历。目标岗位：{jd}。简历内容：{resume}"
    return (
        "请评审以下简历。简历未注明求职意向，请先从简历内容推断其目标岗位/求职意向，"
        f"再据此开展评审。简历内容：{resume}"
    )


def _is_internal_line(line: str, normalized: str) -> bool:
    """该行是否为单行内部元信息（只跳过本行）。"""
    if any(normalized.startswith(marker) for marker in _INTERNAL_LINE_STARTS):
        return True
    if any(token in line for token in _INTERNAL_LINE_CONTAINS):
        return True
    if "字" in normalized and _SELF_CHECK_PATTERN.search(normalized):
        return True
    # 引用其他 agent 编号且带取舍/依据措辞的 deliberation 行（中英文）
    if _AGENT_REF_PATTERN.search(normalized) and _DELIBERATION_WORD_PATTERN.search(normalized):
        return True
    return False


def _find_body_start(lines: list[str]) -> int | None:
    """定位正文起点围栏的行号；没有围栏返回 None。"""
    for i, line in enumerate(lines):
        normalized = _normalize_line(line)
        if normalized and any(p.match(normalized) for p in _BODY_START_PATTERNS):
            return i
    return None


def _clean_lines(lines: list[str]) -> str:
    kept: list[str] = []
    for line in lines:
        normalized = _normalize_line(line)
        if not normalized:
            kept.append(line)
            continue
        # 段落级内部区块标题：其后整块都是内部内容，截断
        if any(normalized.startswith(marker) for marker in _INTERNAL_SECTION_HEADINGS):
            break
        # 单行内部元信息：只跳过本行（可能出现在正文头部，不能截断）
        if _is_internal_line(line, normalized):
            continue
        kept.append(line)
    text = "\n".join(kept).strip()
    # 去掉末尾残留的分隔线
    while text.endswith("---"):
        text = text[: -3].rstrip()
    return text


def _clean_review_content(raw: str) -> str:
    """去掉 agent 追加的内部协作记录/工作区路径，只保留评审正文。

    围栏优先：检测到显式正文围栏时，围栏之前的“交付汇报头”整体丢弃；
    无围栏时从首行开始扫描。两种模式下元信息行都只跳过本行，只有段落级
    区块标题才截断。
    """
    lines = raw.splitlines()
    start = _find_body_start(lines)
    if start is not None:
        return _clean_lines(lines[start + 1 :])
    return _clean_lines(lines)


def _latest_answer(agent_dir: Path) -> str:
    answer_files = sorted(agent_dir.glob("*/answer.txt"))
    if not answer_files:
        return ""
    return _clean_review_content(answer_files[-1].read_text(encoding="utf-8", errors="replace"))


def _agent_review_content(attempt_dir: Path, agent_id: str) -> str:
    """取某 agent 的评审正文。

    优先 final/<agent>/answer.txt —— 那是编排结束时 winner 的最终展示稿
    （coordination_tracker 只为 winner 写 final/）；不存在则回退该 agent
    时间戳最新一轮的 answer.txt（旧行为，覆盖非 winner 与老日志）。
    """
    final_file = attempt_dir / "final" / agent_id / "answer.txt"
    if final_file.exists():
        return _clean_review_content(final_file.read_text(encoding="utf-8", errors="replace"))
    return _latest_answer(attempt_dir / agent_id)


def _latest_votes(attempt_dir: Path) -> list[dict]:
    """每个 voter 只保留时间戳最新的一张 vote.json。"""
    latest_by_voter: dict[str, tuple[str, dict]] = {}
    for vote_file in attempt_dir.glob("*/*/vote.json"):
        try:
            data = json.loads(vote_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        voter = data.get("voter_id")
        if not voter:
            continue
        ts = vote_file.parent.name  # 形如 20260923_220205_449788
        if voter not in latest_by_voter or ts > latest_by_voter[voter][0]:
            latest_by_voter[voter] = (ts, data)

    votes: list[dict] = []
    for voter, (_, data) in sorted(latest_by_voter.items()):
        votes.append(
            {
                "voter": voter,
                "target": data.get("voted_for", ""),
                "reason": data.get("reason", ""),
                "coordination_round": data.get("coordination_round"),
            }
        )
    return votes


def _parse_results(run_dir: Path, stdout_text: str) -> dict:
    attempt_dir = run_dir / "turn_1" / "attempt_1"

    # winner：优先 stdout，其次 final 目录
    winner_match = re.search(r"^WINNER:\s*(\S+)", stdout_text, flags=re.MULTILINE)
    winner = winner_match.group(1) if winner_match else ""
    if not winner:
        final_dir = attempt_dir / "final"
        if final_dir.exists():
            final_agents = [p.name for p in final_dir.iterdir() if p.is_dir()]
            winner = final_agents[0] if final_agents else ""

    reviews = []
    if attempt_dir.exists():
        for agent_id, dimension in AGENT_DIMENSIONS.items():
            reviews.append(
                {
                    "agent_id": agent_id,
                    "dimension": dimension,
                    "content": _agent_review_content(attempt_dir, agent_id),
                }
            )

    votes = _latest_votes(attempt_dir) if attempt_dir.exists() else []

    return {
        "reviews": reviews,
        "votes": votes,
        "winner": winner,
    }


async def run_resume_review(task_id: str, resume: str, jd: str | None) -> dict:
    """执行一次完整的三智能体简历评审，返回聚合结果 dict。"""
    prompt = build_prompt(resume=resume, jd=jd)
    cmd = _build_command(prompt)

    # 注意：不能用 asyncio.create_subprocess_exec —— uvicorn --reload 在
    # Windows 上是 SelectorEventLoop，不支持 asyncio 子进程（NotImplementedError）。
    # _exec_command 内部走线程池 + 同步 subprocess，等待期间事件循环照常服务。
    stdout_text, returncode = await _exec_command(
        cmd, cwd=str(PROJECT_ROOT), timeout=_SUBPROCESS_TIMEOUT_SECONDS
    )

    log_match = re.search(r"^LOG_DIR:\s*(.+)$", stdout_text, flags=re.MULTILINE)
    if not log_match:
        tail = "\n".join(stdout_text.splitlines()[-30:])
        raise RuntimeError(f"未能从 massgen 输出中定位 LOG_DIR，子进程退出码 {returncode}。\n输出末尾：\n{tail}")

    log_dir_raw = log_match.group(1).strip()
    run_dir = Path(log_dir_raw)
    if not run_dir.is_absolute():
        run_dir = PROJECT_ROOT / run_dir

    parsed = _parse_results(run_dir, stdout_text)

    # 不能静默返回空结果：子进程非零退出或三个维度全空时（如 configuration_error
    # 崩在编排前），必须带着 massgen 记录的失败原因抛出，否则前端只会显示
    # "Winner（无）/ 该维度未产出"，看起来像成功却什么都没有。
    produced = [r["agent_id"] for r in parsed["reviews"] if r["content"].strip()]
    if returncode != 0 or not produced:
        reason = _read_failure_reason(run_dir)
        tail = "\n".join(stdout_text.splitlines()[-15:])
        detail = reason or f"输出末尾：\n{tail}"
        raise RuntimeError(
            f"评审未产出（子进程退出码 {returncode}，产出维度：{produced or '无'}）。{detail}"
        )

    return {
        "task_id": task_id,
        "jd": jd,
        "reviews": parsed["reviews"],
        "votes": parsed["votes"],
        "winner": parsed["winner"],
        "note": NOTE,
        "log_dir": str(run_dir).replace("\\", "/"),
        "exit_code": returncode,
    }


def new_task_id() -> str:
    return uuid.uuid4().hex
