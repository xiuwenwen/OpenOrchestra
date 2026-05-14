from __future__ import annotations

from typing import Any

from harness.gates.test_gate import TestGateService


class RuntimeReadinessGateService:
    def __init__(self, *, config: dict[str, Any], test_gate_service: TestGateService):
        self.config = config
        self.test_gate_service = test_gate_service

    def run(self, task_id: str, round_id: int) -> bool:
        runtime_config = self.config.get("runtime_readiness", {})
        require_commands = bool(runtime_config.get("require_commands", False)) if isinstance(runtime_config, dict) else False
        return self.test_gate_service.run_gate(
            task_id,
            round_id,
            artifact_type="runtime_readiness.md",
            title="Runtime Readiness Gate",
            log_dir_name="runtime_readiness_logs",
            commands=None,
            require_commands=require_commands,
        )
