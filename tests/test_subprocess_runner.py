from __future__ import annotations

import sys
from pathlib import Path

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
