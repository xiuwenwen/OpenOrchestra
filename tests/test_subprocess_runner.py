from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from harness.adapters.subprocess_runner import SubprocessRunner


def test_subprocess_runner_handles_timeout_bytes(tmp_path: Path) -> None:
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"

    exit_code = SubprocessRunner().run(
        [sys.executable, "-c", "import time; print('partial stdout', flush=True); time.sleep(2)"],
        tmp_path,
        0.01,
        stdout_path,
        stderr_path,
    )

    assert exit_code == 124
    assert "TIMEOUT" in stderr_path.read_text(encoding="utf-8")


def test_subprocess_runner_merges_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXISTING_ENV", "yes")
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"

    exit_code = SubprocessRunner().run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('EXISTING_ENV')); print(os.environ.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS'))",
        ],
        tmp_path,
        0,
        stdout_path,
        stderr_path,
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16000"},
    )

    assert exit_code == 0
    assert stdout_path.read_text(encoding="utf-8").splitlines() == ["yes", "16000"]


def test_subprocess_runner_can_stream_output_while_writing_logs(tmp_path: Path, capsys) -> None:
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"

    exit_code = SubprocessRunner(stream_output=True, stream_prefix="[agent] ").run(
        [
            sys.executable,
            "-c",
            "import sys; print('live stdout', flush=True); print('live stderr', file=sys.stderr, flush=True)",
        ],
        tmp_path,
        0,
        stdout_path,
        stderr_path,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[agent] live stdout" in captured.out
    assert "[agent] live stderr" in captured.err
    assert stdout_path.read_text(encoding="utf-8").strip() == "live stdout"
    assert stderr_path.read_text(encoding="utf-8").strip() == "live stderr"
    assert "[stdout] live stdout" in (tmp_path / "live.log").read_text(encoding="utf-8")


def test_subprocess_runner_starts_posix_process_group(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    popen_kwargs: dict[str, object] = {}

    class FakeStream:
        def readline(self):
            return ""

        def close(self):
            return None

    class FakeProcess:
        pid = 12345
        stdin = None
        stdout = FakeStream()
        stderr = FakeStream()

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("harness.adapters.subprocess_runner.subprocess.Popen", fake_popen)

    exit_code = SubprocessRunner().run(
        ["echo", "ok"],
        tmp_path,
        0,
        tmp_path / "stdout.log",
        tmp_path / "stderr.log",
    )

    assert exit_code == 0
    assert popen_kwargs["start_new_session"] is (
        os.name == "posix" and hasattr(os, "setsid") and hasattr(os, "killpg")
    )


def test_subprocess_runner_kills_process_tree_on_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    killed: list[int] = []

    class FakeStream:
        def readline(self):
            return ""

        def close(self):
            return None

    class FakeProcess:
        pid = 12345
        stdin = None
        stdout = FakeStream()
        stderr = FakeStream()

        def wait(self, timeout=None):
            raise KeyboardInterrupt

        def poll(self):
            return None

    monkeypatch.setattr("harness.adapters.subprocess_runner.subprocess.Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr("harness.adapters.subprocess_runner.kill_process_tree", lambda process: killed.append(process.pid))

    with pytest.raises(KeyboardInterrupt):
        SubprocessRunner().run(
            ["sleep", "60"],
            tmp_path,
            0,
            tmp_path / "stdout.log",
            tmp_path / "stderr.log",
        )

    assert killed == [12345]
