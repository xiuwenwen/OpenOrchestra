from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from harness.core.errors import TaskFailedError
from harness.testing.detection import ProjectProfile
from harness.testing.evidence import TestRunEvidence


@dataclass(frozen=True)
class RuntimeContext:
    runtime: str
    host_repo_dir: Path | None
    container_repo_dir: str = "/workspace"
    image: str = ""


@dataclass(frozen=True)
class TestCommand:
    command: str
    scope: str = "host"


@dataclass(frozen=True)
class TestRunRequest:
    __test__ = False

    repo_dir: Path | None
    commands: tuple[str | TestCommand, ...]
    setup_commands: tuple[str | TestCommand, ...]
    log_dir: Path
    timeout_seconds: int
    profile: ProjectProfile
    config: dict[str, Any]
    purpose: str = "test_gate"
    runtime_context: RuntimeContext | None = None


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


def normalize_test_command(command: str | TestCommand, *, default_scope: str) -> TestCommand:
    if isinstance(command, TestCommand):
        return command
    return TestCommand(str(command), scope=default_scope)
