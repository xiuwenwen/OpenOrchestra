from __future__ import annotations

import json
import sys
import uuid
import re
from pathlib import Path
from concurrent.futures import wait as real_wait

import pytest

from harness.agents import runner as agent_runner_module
from harness.agents.result import AgentRunResult, ArtifactRef
from harness.artifacts.schemas import required_outputs_for
import harness.core.orchestrator as orchestrator_module
from harness.core.orchestrator import Orchestrator
from harness.core.progress import ProgressEvent
from harness.core.state_machine import (
    DELIVERY,
    EXECUTION,
    FAILED,
    FINAL_JUDGEMENT,
    FIXING,
    PATCH_MERGE,
    PLAN_REVIEW,
    PLAN_JUDGEMENT,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEW_JUDGEMENT,
    REVIEWING,
    RUNNING,
    TESTING,
    TEST_JUDGEMENT,
)
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, NEW_PROJECT
from harness.patch.gate import materialized_repo_markdown, run_patch_gate


from orchestrator_mock_support import _config


def test_orchestrator_mock_flow_completes_and_generates_delivery(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    task_id = orchestrator.create_task("implement a simple task")

    final_delivery = orchestrator.run_task(task_id)

    task = orchestrator.repository.get_task(task_id)
    task_started = next(event for event in events if event.event_type == "task_started")
    assert task_started.status == RUNNING
    assert task["status"] == "COMPLETED"
    assert final_delivery.exists()
    assert final_delivery.name == "final_delivery.md"
    assert "completed" in final_delivery.read_text(encoding="utf-8")
    phases = [phase["phase_type"] for phase in orchestrator.repository.list_phases(task_id)]
    assert FINAL_JUDGEMENT not in phases
    assert orchestrator.repository.list_artifacts(task_id, "final_delivery.md")
    usage_guides = orchestrator.repository.list_artifacts(task_id, "usage_guide.md")
    assert usage_guides
    usage_guide = Path(usage_guides[-1]["path"])
    assert usage_guide.exists()
    assert "How To Use The Delivery" in usage_guide.read_text(encoding="utf-8")
    planner_run = next(run for run in orchestrator.repository.list_agent_runs(task_id) if run["role"] == "planner")
    planner_phase = next(phase for phase in orchestrator.repository.list_phases(task_id) if phase["phase_id"] == planner_run["phase_id"])
    prompt_path = (
        Path(config["system"]["workspace_root"])
        / task_id
        / planner_run["phase_id"]
        / "planner"
        / planner_run["agent_id"]
        / f"round_{planner_phase['round_id']}"
        / f"attempt_{planner_run['retry_count']}"
        / "logs"
        / "prompt.md"
    )
    assert prompt_path.exists()
    assert "Role: planner" in prompt_path.read_text(encoding="utf-8")

def test_failed_exhausted_new_project_continue_appends_new_fix_window(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["limits"]["max_test_fix_rounds"] = 2
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("build a project", workflow_type=NEW_PROJECT)
    for round_id in (0, 1, 2):
        phase_type = EXECUTION if round_id == 0 else FIXING
        orchestrator.repository.create_phase(task_id, phase_type, "executor", round_id, status="COMPLETED")
        orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", round_id, status="COMPLETED")
        orchestrator.repository.create_phase(task_id, TESTING, "tester", round_id, status="COMPLETED")
        orchestrator.repository.create_phase(task_id, TEST_JUDGEMENT, "judge", round_id, status="COMPLETED")
    orchestrator.repository.update_task(task_id, status=FAILED, current_phase=TEST_JUDGEMENT, current_role="judge")
    called: list[tuple[str, int]] = []
    validation_rounds: list[int] = []
    orchestrator._active_task_id = task_id
    orchestrator._active_task_resume_status = FAILED

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        called.append((phase, round_id))
        return []

    def fake_patch_merge(task_id: str, round_id: int, user_prompt: str) -> bool:
        validation_rounds.append(round_id)
        return False

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)
    monkeypatch.setattr(orchestrator, "run_patch_merge", fake_patch_merge)

    try:
        orchestrator._run_execution_test_loop(task_id, "build a project")
    except orchestrator_module.TaskFailedError:
        pass

    assert called == [(FIXING, 3), (FIXING, 4)]
    assert validation_rounds == [3, 4]

def test_unlimited_new_project_test_fix_rounds_continue_until_pass(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["limits"]["max_test_fix_rounds"] = "unlimited"
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("build a project", workflow_type=NEW_PROJECT)
    validation_rounds: list[int] = []
    tested_rounds: list[int] = []

    def fake_patch_merge(task_id: str, round_id: int, user_prompt: str) -> bool:
        validation_rounds.append(round_id)
        return True

    def fake_test_gate(task_id: str, round_id: int) -> bool:
        tested_rounds.append(round_id)
        return True

    monkeypatch.setattr(orchestrator, "run_patch_merge", fake_patch_merge)
    monkeypatch.setattr(orchestrator, "run_harness_test_gate", fake_test_gate)
    monkeypatch.setattr(orchestrator, "run_judge_phase", lambda *args, **kwargs: {"decision": "pass"})
    monkeypatch.setattr(orchestrator.judge, "is_test_pass", lambda decision: len(tested_rounds) >= 8)

    orchestrator._run_execution_test_loop(task_id, "build a project")

    assert validation_rounds == list(range(8))
    assert tested_rounds == list(range(8))

def test_unlimited_failed_new_project_resume_starts_after_highest_round(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["limits"]["max_test_fix_rounds"] = "unlimited"
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("build a project", workflow_type=NEW_PROJECT)
    for round_id in range(8):
        phase_type = EXECUTION if round_id == 0 else FIXING
        orchestrator.repository.create_phase(task_id, phase_type, "executor", round_id, status="COMPLETED")
        orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", round_id, status="COMPLETED")
        orchestrator.repository.create_phase(task_id, TESTING, "tester", round_id, status="COMPLETED")
        orchestrator.repository.create_phase(task_id, TEST_JUDGEMENT, "judge", round_id, status="COMPLETED")
    orchestrator._active_task_id = task_id
    orchestrator._active_task_resume_status = FAILED
    called: list[tuple[str, int]] = []
    validation_rounds: list[int] = []

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        called.append((phase, round_id))
        return []

    def fake_patch_merge(task_id: str, round_id: int, user_prompt: str) -> bool:
        validation_rounds.append(round_id)
        return True

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)
    monkeypatch.setattr(orchestrator, "run_patch_merge", fake_patch_merge)
    monkeypatch.setattr(orchestrator, "run_harness_test_gate", lambda *args, **kwargs: True)
    monkeypatch.setattr(orchestrator, "run_judge_phase", lambda *args, **kwargs: {"decision": "pass"})
    monkeypatch.setattr(orchestrator.judge, "is_test_pass", lambda decision: True)

    orchestrator._run_execution_test_loop(task_id, "build a project")

    assert called[:2] == [(FIXING, 8), (TESTING, 8)]
    assert validation_rounds == [8]

def test_default_test_fix_round_limit_is_ten(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["limits"].pop("max_test_fix_rounds")
    orchestrator = Orchestrator(config)

    assert orchestrator._max_test_fix_rounds() == 10

def test_test_fix_round_limit_callback_can_extend_execution_loop(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["limits"]["max_test_fix_rounds"] = 1
    events: list[ProgressEvent] = []
    choices = ["extra_10"]
    orchestrator = Orchestrator(
        config,
        progress_callback=events.append,
        fix_round_limit_callback=lambda task_id, current_limit: choices.pop(0),
    )
    task_id = orchestrator.create_task("build a project", workflow_type=NEW_PROJECT)
    fix_rounds: list[int] = []
    tested_rounds: list[int] = []

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        if phase == FIXING:
            fix_rounds.append(round_id)
        return []

    def fake_judge_phase(task_id: str, phase: str, round_id: int, user_prompt: str) -> dict[str, str]:
        return {"decision": "pass" if round_id >= 2 else "fail"}

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)
    monkeypatch.setattr(orchestrator, "run_patch_merge", lambda *args, **kwargs: True)
    monkeypatch.setattr(orchestrator, "run_harness_test_gate", lambda task_id, round_id: tested_rounds.append(round_id) or True)
    monkeypatch.setattr(orchestrator, "run_judge_phase", fake_judge_phase)
    monkeypatch.setattr(orchestrator.judge, "is_test_pass", lambda decision: decision.get("decision") == "pass")

    orchestrator._run_execution_test_loop(task_id, "build a project")

    assert fix_rounds == [1, 2]
    assert tested_rounds == [0, 1, 2]
    assert any(event.event_type == "test_fix_round_limit_reached" and "已达最大修复轮次(1)" in (event.message or "") for event in events)

def test_regression_round_id_does_not_depend_on_mutable_fix_limit(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))

    assert orchestrator.workflow_engine.regression_phase_round_id(2, 3, 5) == 5
    assert orchestrator.workflow_engine.regression_phase_round_id(2, 3, 15) == 5
    assert orchestrator.workflow_engine.regression_phase_round_id(2, 3, None) == 5

def test_regression_round_ids_stay_stable_after_fix_limit_extension(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["limits"]["max_test_fix_rounds"] = 1
    choices = ["extra_10"]
    orchestrator = Orchestrator(
        config,
        fix_round_limit_callback=lambda task_id, current_limit: choices.pop(0),
    )
    task_id = orchestrator.create_task("review fix after extension", workflow_type=BUGFIX)
    role_calls: list[tuple[str, int]] = []
    phase_scopes: list[dict[str, int | str | None] | None] = []
    gate_rounds: list[int] = []
    patch_rounds: list[int] = []

    def fake_run_role_phase(
        role: str,
        phase: str,
        round_id: int,
        required_outputs: list[str],
        user_prompt: str,
        **kwargs,
    ) -> list[AgentRunResult]:
        role_calls.append((phase, round_id))
        phase_scopes.append(kwargs.get("phase_scope"))
        return []

    def fake_patch_merge(task_id: str, round_id: int, user_prompt: str) -> bool:
        patch_rounds.append(round_id)
        return True

    def fake_judge_phase(task_id: str, phase: str, round_id: int, user_prompt: str) -> dict[str, str]:
        return {"decision": "pass" if round_id == 3 else "fail"}

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)
    monkeypatch.setattr(orchestrator, "run_patch_merge", fake_patch_merge)
    monkeypatch.setattr(orchestrator, "run_harness_test_gate", lambda task_id, round_id: gate_rounds.append(round_id) or True)
    monkeypatch.setattr(orchestrator, "run_judge_phase", fake_judge_phase)
    monkeypatch.setattr(orchestrator.judge, "is_test_pass", lambda decision: decision.get("decision") == "pass")

    orchestrator._run_regression_test_fix_loop(task_id, "review fix after extension", review_round_id=1, merge_ok=True)

    assert gate_rounds == [1, 2, 3]
    assert patch_rounds == [2, 3]
    assert [round_id for phase, round_id in role_calls if phase == REVIEW_FIXING] == [2, 3]
    assert [
        scope
        for (phase, _round_id), scope in zip(role_calls, phase_scopes)
        if phase == REVIEW_FIXING
    ] == [
        {"loop_type": "regression_test_fix", "parent_round_id": 1, "iteration_id": 1},
        {"loop_type": "regression_test_fix", "parent_round_id": 1, "iteration_id": 2},
    ]

def test_same_role_agents_start_concurrently_when_configured(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["mock"] = {"delay_seconds": 0.05}
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    orchestrator.create_task("plan concurrent work")

    orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, required_outputs_for("planner", PLANNING_DRAFT), "plan concurrent work")

    relevant = [
        event
        for event in events
        if event.role == "planner" and event.event_type in {"agent_started", "agent_completed"}
    ]
    assert [event.event_type for event in relevant[:2]] == ["agent_started", "agent_started"]
    assert {event.agent_id for event in relevant if event.event_type == "agent_started"} == {"planner-1", "planner-2"}

def test_fix_phases_force_single_executor_even_when_configured_for_multiple(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["executor"]["count"] = 3
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    orchestrator.create_task("fix one defect")

    for phase in (FIXING, REVIEW_FIXING):
        results = orchestrator.run_role_phase(
            "executor",
            phase,
            0,
            required_outputs_for("executor", phase),
            "fix one defect",
            agent_count_override=4,
        )

        assert [result.agent_id for result in results] == ["executor-1"]

    started = [
        event
        for event in events
        if event.role == "executor" and event.event_type == "phase_started" and event.phase in {FIXING, REVIEW_FIXING}
    ]
    assert [event.message for event in started] == [
        "FIXING started with 1 executor agent(s)",
        "REVIEW_FIXING started with 1 executor agent(s)",
    ]

def test_concurrent_phase_timeout_budget_includes_agent_retries(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["timeouts"]["planner"] = 7
    config["limits"]["max_agent_retry"] = 2
    captured_timeouts: list[float | None] = []
    orchestrator = Orchestrator(config)
    orchestrator.create_task("plan retry budget")

    def tracking_wait(futures, timeout=None):
        captured_timeouts.append(timeout)
        return real_wait(futures, timeout=timeout)

    monkeypatch.setattr(agent_runner_module, "wait", tracking_wait)

    orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, required_outputs_for("planner", PLANNING_DRAFT), "plan retry budget")

    assert captured_timeouts == [51]

def test_concurrent_phase_has_no_wait_timeout_when_role_timeout_is_zero(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["timeouts"]["planner"] = 0
    captured_timeouts: list[float | None] = []
    orchestrator = Orchestrator(config)
    orchestrator.create_task("plan without timeout")

    def tracking_wait(futures, timeout=None):
        captured_timeouts.append(timeout)
        return real_wait(futures, timeout=timeout)

    monkeypatch.setattr(agent_runner_module, "wait", tracking_wait)

    orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, required_outputs_for("planner", PLANNING_DRAFT), "plan without timeout")

    assert captured_timeouts == [None]

def test_orchestrator_uses_task_configuration_for_backend_counts_and_timeout(tmp_path: Path) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("task-scoped config")
    orchestrator.repository.update_task_configuration(
        task_id,
        '{"agent_backend":{"planner":"claude"},"roles":{"planner":{"count":1}},"timeouts":{"planner":9}}',
    )

    assert orchestrator._backend_for(task_id, "planner") == "claude"
    assert orchestrator._effective_agent_count(task_id, "planner", PLANNING_DRAFT) == 1
    assert orchestrator.config_service.timeout_for(task_id, "planner") == 9
