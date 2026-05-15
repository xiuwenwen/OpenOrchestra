from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness.adapters.base import AgentAdapter
from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult
from harness.core.orchestrator import Orchestrator
from harness.core.scheduler import BackendBulkheadScheduler
from harness.core.state_machine import PLANNING_DRAFT


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
            "planner": {"count": 2},
            "executor": {"count": 1},
            "tester": {"count": 1},
            "reviewer": {"count": 1},
            "communicator": {"count": 1},
        },
        "limits": {"max_agent_retry": 0},
        "timeouts": {"planner": 5},
        "policy": {"same_role_can_run_concurrently": True},
        "heartbeat": {"interval_seconds": 0},
        "backend_concurrency": {"claude": 1},
        "artifact_input": {"max_files": 10, "max_file_bytes": 4096, "max_total_bytes": 65536},
    }


def test_backend_bulkhead_limits_same_backend_concurrency() -> None:
    scheduler = BackendBulkheadScheduler(backend_limits={"claude": 1})
    active = 0
    max_active = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal active, max_active
        with scheduler.acquire(backend="claude", role="planner"):
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: worker(), range(2)))

    assert max_active == 1


def test_agent_runner_uses_backend_bulkhead_for_parallel_agents(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    orchestrator.create_task("parallel planners")
    active = 0
    max_active = 0
    calls = 0
    lock = threading.Lock()

    class SlowAdapter(AgentAdapter):
        def run(self, context: AgentRunContext) -> AgentRunResult:
            nonlocal active, max_active, calls
            with lock:
                active += 1
                calls += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return AgentRunResult(
                context.task_id,
                context.phase_id,
                context.role,
                context.agent_id,
                "COMPLETED",
                exit_code=0,
            )

    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: SlowAdapter())

    orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, [], "parallel planners")

    assert calls == 2
    assert max_active == 1
