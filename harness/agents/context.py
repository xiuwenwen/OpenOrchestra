from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.runtime.spec import RuntimeSpec


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
    metadata: dict[str, Any] = field(default_factory=dict)
    runtime_spec: RuntimeSpec = field(default_factory=RuntimeSpec)
    runtime_workspace_dir: str = ""
    runtime_repo_dir: str = ""
    runtime_input_dir: str = ""
    runtime_output_dir: str = ""
    runtime_log_dir: str = ""
    retry_feedback: list[str] = field(default_factory=list)

    @property
    def runtime_mode(self) -> str:
        return self.runtime_spec.mode

    def runtime_path(self, path: Path) -> str:
        """Return the path an agent should use inside its execution runtime."""
        if not self.runtime_spec.is_docker:
            return str(path)
        resolved = path.resolve()
        for host_root, runtime_root in (
            (self.repo_dir, self.runtime_repo_dir),
            (self.input_dir, self.runtime_input_dir),
            (self.output_dir, self.runtime_output_dir),
            (self.log_dir, self.runtime_log_dir),
            (self.workspace_dir, self.runtime_workspace_dir),
        ):
            if not runtime_root:
                continue
            try:
                relative = resolved.relative_to(host_root.resolve())
            except ValueError:
                continue
            if str(relative) == ".":
                return runtime_root
            return f"{runtime_root.rstrip('/')}/{relative.as_posix()}"
        return str(path)
