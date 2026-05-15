from __future__ import annotations

from pathlib import Path

import pytest

from harness.adapters.base import AgentAdapter
from harness.adapters.health import BackendHealthMonitor
from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult
from harness.contracts.role_contracts import required_outputs_for
from harness.core.errors import TaskFailedError
from harness.core.progress import ProgressEvent
from harness.core.state_machine import PLANNING_DRAFT
from harness.core.orchestrator import Orchestrator


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
        },
        "agent_backend": {
            "default": "claude",
            "planner": "claude",
            "executor": "claude",
            "tester": "claude",
            "reviewer": "claude",
            "communicator": "claude",
        },
        "roles": {
            "planner": {"count": 1},
            "executor": {"count": 1},
            "tester": {"count": 1},
            "reviewer": {"count": 1},
            "communicator": {"count": 1},
        },
        "limits": {"max_agent_retry": 2},
        "timeouts": {"planner": 5},
        "policy": {"same_role_can_run_concurrently": False},
        "heartbeat": {"interval_seconds": 0},
        "backend_health": {"failure_threshold": 1, "cooldown_seconds": 30},
        "artifact_input": {"max_files": 10, "max_file_bytes": 4096, "max_total_bytes": 65536},
    }


def test_backend_health_monitor_opens_and_recovers_after_cooldown() -> None:
    now = [0.0]
    monitor = BackendHealthMonitor(failure_threshold=2, cooldown_seconds=10, time_provider=lambda: now[0])

    assert monitor.check("claude").allowed
    degraded = monitor.record_failure("claude", "Command timed out after 30s.", status="FAILED")
    assert degraded.state == "degraded"
    assert degraded.allowed

    opened = monitor.record_failure("claude", "exit_code=124", status="FAILED")
    assert opened.state == "open"
    assert not opened.allowed
    assert not monitor.check("claude").allowed

    now[0] = 11.0
    probe = monitor.check("claude")
    assert probe.state == "degraded"
    assert probe.allowed

    recovered = monitor.record_success("claude")
    assert recovered.state == "healthy"
    assert recovered.consecutive_failures == 0


def test_backend_health_auth_opens_immediately_and_contract_errors_do_not_poison_backend() -> None:
    monitor = BackendHealthMonitor(failure_threshold=5, cooldown_seconds=10)

    ignored = monitor.record_failure("claude", "Missing required output: bug_report.md", status="OUTPUT_INVALID")
    assert ignored.state == "healthy"
    assert ignored.allowed

    opened = monitor.record_failure("claude", "401 unauthorized invalid api key", status="FAILED")
    assert opened.state == "open"
    assert opened.failure_kind == "auth"
    assert not opened.allowed


def test_backend_health_monitor_persists_open_state_across_instances() -> None:
    now = [100.0]
    persisted: dict[str, dict[str, object]] = {}

    def save(snapshot) -> None:
        persisted[snapshot.backend] = {
            "state": snapshot.state,
            "consecutive_failures": snapshot.consecutive_failures,
            "failure_kind": snapshot.failure_kind,
            "open_until": snapshot.open_until,
            "reason": snapshot.reason,
        }

    monitor = BackendHealthMonitor(
        failure_threshold=1,
        cooldown_seconds=30,
        time_provider=lambda: now[0],
        persist_callback=save,
    )
    monitor.record_failure("claude", "Command timed out.", status="FAILED")

    reloaded = BackendHealthMonitor(
        failure_threshold=1,
        cooldown_seconds=30,
        time_provider=lambda: now[0],
        persisted_states=persisted,
        persist_callback=save,
    )

    assert not reloaded.check("claude").allowed

    now[0] = 131.0
    assert reloaded.check("claude").allowed
    assert persisted["claude"]["state"] == "degraded"


def test_backend_health_monitor_supports_manual_cooldown() -> None:
    now = [10.0]
    monitor = BackendHealthMonitor(time_provider=lambda: now[0])

    snapshot = monitor.cooldown_backend("claude", 45)

    assert snapshot.state == "open"
    assert snapshot.failure_kind == "manual_cooldown"
    assert snapshot.open_until == 55.0
    assert not monitor.check("claude").allowed


def test_agent_runner_stops_retries_when_backend_circuit_opens(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    task_id = orchestrator.create_task("plan with a failing backend")
    calls: list[str] = []

    class FailingAdapter(AgentAdapter):
        def run(self, context: AgentRunContext) -> AgentRunResult:
            calls.append(context.agent_id)
            return AgentRunResult(
                context.task_id,
                context.phase_id,
                context.role,
                context.agent_id,
                "FAILED",
                exit_code=1,
            )

    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: FailingAdapter())

    with pytest.raises(TaskFailedError, match="circuit opened"):
        orchestrator.run_role_phase(
            "planner",
            PLANNING_DRAFT,
            0,
            required_outputs_for("planner", PLANNING_DRAFT),
            "plan with a failing backend",
        )

    assert calls == ["planner-1"]
    assert len(orchestrator.repository.list_agent_runs(task_id)) == 1
    assert any(
        event.event_type == "backend_health_changed"
        and event.status == "OPEN"
        and event.data.get("backend") == "claude"
        for event in events
    )
