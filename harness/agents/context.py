from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentRunContext:
    task_id: str
    phase_id: str
    phase: str
    role: str
    agent_id: str
    round_id: int
    user_prompt: str
    role_instruction: str
    workspace_dir: Path
    repo_dir: Path
    input_dir: Path
    output_dir: Path
    log_dir: Path
    input_artifacts: list[Path] = field(default_factory=list)
    required_outputs: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    config: dict[str, Any] = field(default_factory=dict)

