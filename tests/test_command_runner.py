from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness.adapters.command_runner import CommandRunner


def test_command_runner_captures_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    result = CommandRunner().run_capture(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
        ],
        cwd=tmp_path,
    )

    assert result.returncode == 7
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert not result.timed_out


def test_command_runner_merges_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OO_EXISTING_ENV", "base")

    result = CommandRunner().run_capture(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('OO_EXISTING_ENV')); print(os.environ.get('OO_EXTRA_ENV'))",
        ],
        cwd=tmp_path,
        env={"OO_EXTRA_ENV": "extra"},
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["base", "extra"]


def test_command_runner_reports_timeout_without_raising(tmp_path: Path) -> None:
    result = CommandRunner().run_capture(
        [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(2)"],
        cwd=tmp_path,
        timeout_seconds=0.01,
    )

    assert result.returncode == 124
    assert result.timed_out
    assert "Command timed out" in result.stderr


def test_command_runner_rejects_string_commands(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty argv list"):
        CommandRunner().run_capture("echo unsafe", cwd=tmp_path)  # type: ignore[arg-type]
