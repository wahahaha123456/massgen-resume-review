"""清洗器 + final 优先读取的离线回归测试（纯离线，不调用 LLM）。

覆盖：
- 10 份历史 answer.txt fixtures：清洗后必须非空、正文关键句保留、内部元信息剥离
- _parse_results：winner 优先读 final/<agent>/answer.txt，其余 agent 回退最后一轮

运行：
    cd d:\\MassGen
    uv run pytest api/test_cleaner_fixtures.py -v
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.massgen_runner import _clean_review_content, _parse_results, build_prompt  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "cleaner"
CASES: dict = json.loads((FIXTURE_DIR / "cases.json").read_text(encoding="utf-8"))

# 任何一份评审清洗后都不得残留的内部协作元信息（字面量）
GLOBAL_BANNED = [
    "工作区：",
    "工作目录：",
    "changedoc.md",
    "deliverable/",
    "Task Status",
    "Vote 评估",
    "评审说明",
    "Change Document",
    "Deliberation",
    "Key Output Changes",
    "MCP 不可用",
    "与既有答案的差异",
    "决策日志",
    "决策记录",
    "决策要点",
    "Open Gaps",
]

# 同上（正则）
GLOBAL_BANNED_PATTERNS = [
    r"agent\d+\.\d+",  # deliberation 中对其他 agent 编号（agent1.1/agent2.2 等）的引用
    r"DEC-0",
    r"workspace_[0-9a-f]+",
    r"[（(]\s*(?:全文|正文)?约?\s*\d+\s*字",  # 字数自检行，如「（全文 297 字…」「（正文约 284 字…」
]


@pytest.mark.parametrize("name", sorted(CASES))
def test_cleaner_fixture(name: str) -> None:
    raw = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    cleaned = _clean_review_content(raw)

    assert cleaned.strip(), f"{name}: 清洗后为空（0 字）"

    for snippet in CASES[name]["must_contain"]:
        assert snippet in cleaned, f"{name}: 正文丢失，未找到 {snippet!r}\n清洗结果：\n{cleaned}"

    for banned in GLOBAL_BANNED + CASES[name].get("must_not_contain", []):
        assert banned not in cleaned, f"{name}: 残留内部元信息 {banned!r}\n清洗结果：\n{cleaned}"

    for pattern in GLOBAL_BANNED_PATTERNS:
        assert re.search(pattern, cleaned) is None, (
            f"{name}: 残留内部元信息（正则 {pattern!r}）\n清洗结果：\n{cleaned}"
        )

    # 清洗只应删减，不应比原文长
    assert len(cleaned) <= len(raw), f"{name}: 清洗后比原文还长"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_parse_results_prefers_final_answer(tmp_path: Path) -> None:
    """winner 有 final/<agent>/answer.txt 时必须优先用它，而不是最后一轮 answer。"""
    attempt = tmp_path / "turn_1" / "attempt_1"

    # tech_reviewer：最后一轮 answer（旧逻辑会读到它）与 final 展示稿内容不同
    _write(
        attempt / "tech_reviewer" / "20260101_000000_000000" / "answer.txt",
        "ROUND 旧稿：技术匹配评分：3/5\n\n工作区：`D:\\should-not-appear`",
    )
    _write(
        attempt / "final" / "tech_reviewer" / "answer.txt",
        "## 技术维度评审\n\n技术匹配评分：5/5 FINAL 展示稿正文\n\n工作区：`D:\\should-not-appear`",
    )
    # 非 winner：没有 final/，回退最后一轮 answer
    _write(
        attempt / "experience_reviewer" / "20260101_000001_000000" / "answer.txt",
        "经历匹配评分：4/5 ROUND 回退正文",
    )
    _write(
        attempt / "improvement_reviewer" / "20260101_000002_000000" / "answer.txt",
        "改进建议 ROUND 回退正文",
    )

    parsed = _parse_results(tmp_path, f"WINNER: tech_reviewer\nLOG_DIR: {tmp_path}")

    assert parsed["winner"] == "tech_reviewer"
    by_agent = {r["agent_id"]: r["content"] for r in parsed["reviews"]}

    assert "FINAL 展示稿正文" in by_agent["tech_reviewer"]
    assert "ROUND 旧稿" not in by_agent["tech_reviewer"]
    # final 稿同样要走清洗器
    assert "工作区：" not in by_agent["tech_reviewer"]
    assert "should-not-appear" not in by_agent["tech_reviewer"]
    # 无 final/ 的 agent 回退最后一轮
    assert "ROUND 回退正文" in by_agent["experience_reviewer"]
    assert "ROUND 回退正文" in by_agent["improvement_reviewer"]


def test_parse_results_falls_back_without_final_dir(tmp_path: Path) -> None:
    """完全没有 final/ 目录时（旧运行日志），行为与历史一致：读最后一轮 answer。"""
    attempt = tmp_path / "turn_1" / "attempt_1"
    for agent in ("tech_reviewer", "experience_reviewer", "improvement_reviewer"):
        _write(
            attempt / agent / "20260101_000000_000000" / "answer.txt",
            f"{agent} 唯一一轮正文 ROUND-ONLY",
        )

    parsed = _parse_results(tmp_path, f"WINNER: tech_reviewer\nLOG_DIR: {tmp_path}")
    by_agent = {r["agent_id"]: r["content"] for r in parsed["reviews"]}
    assert all("ROUND-ONLY" in c for c in by_agent.values())


# ---------- build_prompt：JD 选填 / 留空推断 ----------


def test_build_prompt_with_jd() -> None:
    prompt = build_prompt("简历全文", "Python后端")
    assert "目标岗位：Python后端" in prompt
    assert "推断其目标岗位" not in prompt


def test_build_prompt_jd_none_uses_infer_branch() -> None:
    prompt = build_prompt("简历全文", None)
    assert "推断其目标岗位" in prompt
    assert "目标岗位：" not in prompt


def test_build_prompt_jd_blank_uses_infer_branch() -> None:
    # 空串 / 全空格都视为「未指定」
    for blank in ("", "   ", "　"):
        prompt = build_prompt("简历全文", blank)
        assert "推断其目标岗位" in prompt
        assert "目标岗位：" not in prompt


def test_build_prompt_jd_with_surrounding_spaces() -> None:
    # 带首尾空格的有效 JD：strip 后走 user 分支，prompt 里是去掉空格的值
    prompt = build_prompt("简历全文", "  Python后端  ")
    assert "目标岗位：Python后端" in prompt
    assert "推断其目标岗位" not in prompt
