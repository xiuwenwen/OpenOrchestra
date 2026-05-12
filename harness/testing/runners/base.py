from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from harness.core.errors import TaskFailedError
from harness.testing.detection import ProjectProfile
from harness.testing.evidence import TestRunEvidence


@dataclass(frozen=True)
class TestRunRequest:
    __test__ = False

    repo_dir: Path | None
    commands: tuple[str, ...]
    setup_commands: tuple[str, ...]
    log_dir: Path
    timeout_seconds: int
    profile: ProjectProfile
    config: dict[str, Any]
    purpose: str = "test_gate"


class TestRunner(Protocol):
    runtime: str

    def run(self, request: TestRunRequest) -> TestRunEvidence:
        ...


def split_command(command: str) -> list[str]:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise TaskFailedError(f"Invalid Harness test command: {command!r}: {exc}") from exc
    if not argv:
        raise TaskFailedError("Invalid Harness test command: command is empty")
    return argv
