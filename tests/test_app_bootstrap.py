from __future__ import annotations

from pathlib import Path

from harness.app.bootstrap import build_orchestrator_services
from harness.core.orchestrator import Orchestrator
from harness.core.progress import ProgressEvent


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
        },
        "agent_backend": {"default": "mock"},
        "roles": {},
        "limits": {"max_agent_retry": 1, "max_test_fix_rounds": 1},
        "timeouts": {},
        "policy": {},
    }


class BootstrapOwner:
    _active_task_id: str | None = None
    _active_workflow_type: str | None = None

    def markdown_field(self, content: str, field_name: str) -> str | None:
        prefix = f"{field_name}:"
        for line in content.splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
        return None

    def active_task_id(self) -> str | None:
        return self._active_task_id

    def active_workflow_type(self) -> str | None:
        return self._active_workflow_type

    def emit_progress(self, event: ProgressEvent) -> None:
        self.last_event = event

    def positive_int(self, value, default: int, field_name: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default


def test_bootstrap_wires_orchestrator_services(tmp_path: Path) -> None:
    owner = BootstrapOwner()
    services = build_orchestrator_services(owner, config=_config(tmp_path))

    assert services.agent_runner.orchestrator is owner
    assert services.workflow_engine.runtime is owner
    assert services.materialized_repo_service.repository is services.repository
    assert services.test_gate_service.repository is services.repository
    assert services.patch_gate_service.repository is services.repository
    assert services.input_staging_service.repository is services.repository
    assert services.delivery_publisher.repository is services.repository


def test_orchestrator_constructor_delegates_service_wiring_to_bootstrap() -> None:
    source = Path("harness/core/orchestrator.py").read_text(encoding="utf-8")

    assert "build_orchestrator_services(" in source
    for constructor in (
        "StateRepository(",
        "WorkspaceManager(",
        "ArtifactManager(",
        "MaterializedRepoService(",
        "TestGateService(",
        "PatchGateService(",
        "InputStagingService(",
        "AgentPhaseRunner(",
        "DeliveryPublisher(",
        "WorkflowEngine(",
    ):
        assert constructor not in source


def test_orchestrator_still_constructs_with_default_services(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("bootstrap smoke")

    assert orchestrator.repository.get_task(task_id)["task_id"] == task_id
