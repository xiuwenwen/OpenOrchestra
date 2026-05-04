from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    task_id: str
    phase_id: str | None
    role: str | None
    agent_id: str | None
    artifact_type: str
    path: Path
    version: int
    hash: str | None


@dataclass
class AgentRunResult:
    task_id: str
    phase_id: str
    role: str
    agent_id: str
    status: str
    exit_code: int = 0
    artifacts: list[ArtifactRef] = field(default_factory=list)
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    validation_ok: bool = True
    validation_errors: list[str] = field(default_factory=list)
    need_user_input: bool = False
    user_questions: list[str] = field(default_factory=list)

