"""runner 子进程执行层的离线回归测试（不调 LLM）。

背景：uvicorn 0.36+ 在 Windows 上以 --reload / 多 workers 启动时，
Config.use_subprocess=True，loop 工厂会强制返回 SelectorEventLoop：

    # uvicorn/loops/asyncio.py
    def asyncio_loop_factory(use_subprocess=False):
        if sys.platform == "win32" and not use_subprocess:
            return asyncio.ProactorEventLoop
        return asyncio.SelectorEventLoop

而 Windows 的 SelectorEventLoop 不支持子进程传输，
asyncio.create_subprocess_exec 会抛裸 NotImplementedError（无消息），
导致评审任务秒失败（前端只见 "NotImplementedError:"）。

因此 runner 执行子进程必须与事件循环类型解耦：
统一在工作线程里跑同步 subprocess.run。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from api.massgen_runner import _build_command, _exec_command, _failure_reason_from_metadata

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
ECHO_CMD = [sys.executable, "-c", "print('subprocess-ok')"]


def test_exec_command_under_selector_event_loop() -> None:
    """SelectorEventLoop（uvicorn --reload 的实际循环）下必须能拉起子进程。"""
    if sys.platform != "win32":
        pytest.skip("SelectorEventLoop 不支持子进程是 Windows 专属问题")

    loop = asyncio.SelectorEventLoop()
    try:
        stdout, returncode = loop.run_until_complete(
            _exec_command(ECHO_CMD, cwd=PROJECT_ROOT, timeout=30)
        )
    finally:
        loop.close()

    assert returncode == 0
    assert "subprocess-ok" in stdout


def test_exec_command_under_default_loop() -> None:
    """默认 ProactorEventLoop 下行为不变。"""
    stdout, returncode = asyncio.run(_exec_command(ECHO_CMD, cwd=PROJECT_ROOT, timeout=30))
    assert returncode == 0
    assert "subprocess-ok" in stdout


def test_exec_command_timeout_raises_runtime_error() -> None:
    """超时要转成带明确消息的 RuntimeError，而不是让 TimeoutExpired 裸奔。"""
    slow_cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    with pytest.raises(RuntimeError, match="超时"):
        asyncio.run(_exec_command(slow_cmd, cwd=PROJECT_ROOT, timeout=1))


def test_child_process_stdio_forced_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """子进程 stdio 必须强制 UTF-8。

    中文 Windows 子进程默认 cp936，massgen 启动时 print emoji（🟡/❌）
    会 UnicodeEncodeError 直接崩在配置校验阶段。即便父环境没设
    PYTHONIOENCODING/PYTHONUTF8，runner 也要保证子进程是 UTF-8。
    """
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    # 🟡 在 GBK 中不可编码：非 UTF-8 子进程会抛 UnicodeEncodeError、退出码非 0
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write('🟡 ' + sys.stdout.encoding)"]
    stdout, returncode = asyncio.run(_exec_command(cmd, cwd=PROJECT_ROOT, timeout=15))
    assert returncode == 0, f"子进程编码崩溃：{stdout}"
    assert "🟡" in stdout
    assert "utf-8" in stdout.lower()


def test_command_disables_at_reference_parsing() -> None:
    """简历正文是纯数据（含 mAP@0.5、邮箱等），绝不能让 massgen 的
    @文件引用解析把正文 token 当路径，否则 configuration_error 直接退出。

    真实事故：简历写 "mAP@0.5:0.95 达 22.1%"，@0.5 被当路径，
    子进程报 "Context paths not found: 0.5"、exit 1、三份评审全空。
    """
    cmd = _build_command("请评审以下简历。简历内容：验证集 mAP@0.5:0.95 达 22.1%")
    assert "--no-parse-at-references" in cmd
    # prompt 必须作为单个 argv 传递（list 形式不经 shell）
    assert cmd[-1] == "请评审以下简历。简历内容：验证集 mAP@0.5:0.95 达 22.1%"


def _fake_venv(tmp_path: Path, with_script: bool) -> Path:
    scripts = tmp_path / ("Scripts" if sys.platform == "win32" else "bin")
    scripts.mkdir(parents=True)
    py = scripts / ("python.exe" if sys.platform == "win32" else "python")
    py.write_text("", encoding="utf-8")
    if with_script:
        entry = scripts / ("massgen.exe" if sys.platform == "win32" else "massgen")
        entry.write_text("", encoding="utf-8")
    return py


def test_command_prefers_venv_console_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """直调 venv 的 massgen console script，绕过 `uv run` 的环境解析开销。

    实测热缓存：`uv run massgen --help` 39s，`massgen.exe --help` 24s，
    每次评审白付 ~15s uv 税；console script 与 sys.executable 同目录
    （uvicorn worker / uv run pytest 下 sys.executable 就是 venv python）。
    """
    py = _fake_venv(tmp_path, with_script=True)
    monkeypatch.setattr(sys, "executable", str(py))

    cmd = _build_command("简历正文")
    assert cmd[0] == str(py.parent / ("massgen.exe" if sys.platform == "win32" else "massgen"))
    assert "uv" not in cmd and "run" not in cmd
    assert cmd[1] == "--automation"


def test_command_falls_back_to_uv_without_console_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """console script 不存在时（非常规安装）回退 uv run，保证可用性。"""
    py = _fake_venv(tmp_path, with_script=False)
    monkeypatch.setattr(sys, "executable", str(py))
    monkeypatch.setenv("UV", "X:/fake/uv.exe")

    cmd = _build_command("简历正文")
    assert cmd[:3] == ["X:/fake/uv.exe", "run", "massgen"]
    assert "--automation" in cmd


def test_failure_reason_extracted_from_metadata() -> None:
    """execution_metadata.yaml 里的 failure_stage/failure_error 要能提出来给用户看。"""
    metadata = (
        "cli_args:\n  fast: false\n"
        "  failure_stage: configuration_error\n"
        '  failure_error: "Context paths not found:\\n  - 0.5\\n\\nPlease check"\n'
        "git:\n  commit: abc\n"
    )
    reason = _failure_reason_from_metadata(metadata)
    assert "configuration_error" in reason
    assert "Context paths not found" in reason
    assert "0.5" in reason


def test_failure_reason_empty_when_no_failure() -> None:
    assert _failure_reason_from_metadata("cli_args:\n  fast: false\ngit:\n  commit: abc\n") == ""
    assert _failure_reason_from_metadata("") == ""


@pytest.mark.asyncio
async def test_run_review_raises_when_nothing_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子进程 exit 1 + 空 reviews 时必须抛出含真实原因的错误，不能静默返回空壳。"""
    import api.massgen_runner as runner

    attempt = tmp_path / "turn_1" / "attempt_1"
    attempt.mkdir(parents=True)
    (attempt / "events.jsonl").write_text("", encoding="utf-8")
    (attempt / "execution_metadata.yaml").write_text(
        "cli_args:\n"
        "  failure_stage: configuration_error\n"
        '  failure_error: "Context paths not found:\\n  - 0.5"\n',
        encoding="utf-8",
    )

    async def fake_exec(cmd, cwd, timeout):
        return (f"LOG_DIR: {tmp_path.as_posix()}\n", 1)

    monkeypatch.setattr(runner, "_exec_command", fake_exec)

    with pytest.raises(RuntimeError, match="评审未产出") as exc_info:
        await runner.run_resume_review(
            task_id="t1", resume="含 mAP@0.5 的简历内容至少十字", jd=None
        )
    assert "configuration_error" in str(exc_info.value)
    assert "0.5" in str(exc_info.value)
