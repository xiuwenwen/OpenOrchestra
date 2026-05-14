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
from harness.testing.tester_result import TesterResult as HarnessTesterResult


from orchestrator_mock_support import _config


def _tester_result(tmp_path: Path, status: str) -> HarnessTesterResult:
    next_action = {
        "tests_passed": "continue",
        "source_bug": "fix_code",
        "environment_blocked": "block_task",
    }[status]
    return HarnessTesterResult(status, next_action, status, status, tmp_path / "tester_result.json", {"status": status})


def test_failed_exhausted_bugfix_continue_appends_new_round_window(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["limits"]["max_test_fix_rounds"] = 2
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("fix a failing command", workflow_type=BUGFIX)
    for round_id in (0, 1):
        orchestrator.repository.create_phase(task_id, FIXING, "executor", round_id, status="COMPLETED")
        orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", round_id, status="COMPLETED")
    orchestrator.repository.update_task(task_id, status=FAILED, current_phase=PATCH_MERGE, current_role="executor")
    called_rounds: list[int] = []

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        called_rounds.append(round_id)
        return []

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)
    monkeypatch.setattr(orchestrator, "run_patch_merge", lambda task_id, round_id, user_prompt: False)

    try:
        orchestrator.run_task(task_id, workflow_type=BUGFIX)
    except orchestrator_module.TaskFailedError:
        pass

    assert called_rounds == [2, 3]

def test_unlimited_bugfix_rounds_continue_until_pass(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["limits"]["max_test_fix_rounds"] = "unlimited"
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("fix a failing command", workflow_type=BUGFIX)
    fix_rounds: list[int] = []

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        if phase == FIXING:
            fix_rounds.append(round_id)
        return []

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)
    monkeypatch.setattr(orchestrator, "run_patch_merge", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        orchestrator.workflow_engine,
        "run_testing_until_tester_decision",
        lambda *args, **kwargs: _tester_result(tmp_path, "tests_passed" if len(fix_rounds) >= 7 else "source_bug"),
    )
    monkeypatch.setattr(orchestrator.workflow_engine, "run_bugfix_planning_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator.workflow_engine, "run_review_loop", lambda *args, **kwargs: None)
    delivery = tmp_path / "final_delivery.md"
    delivery.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(orchestrator.workflow_engine, "run_delivery", lambda *args, **kwargs: delivery)

    result = orchestrator._run_bugfix_flow(task_id, "fix a failing command")

    assert result == delivery
    assert fix_rounds == list(range(7))


def test_bugfix_flow_runs_two_planners_before_fixing(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("fix a parsing bug", workflow_type=BUGFIX)
    calls: list[tuple[str, str, int]] = []

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        calls.append((role, phase, round_id))
        return []

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)
    monkeypatch.setattr(orchestrator.workflow_engine, "plan_review_approved", lambda results: True)
    monkeypatch.setattr(orchestrator, "run_patch_merge", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        orchestrator.workflow_engine,
        "run_testing_until_tester_decision",
        lambda *args, **kwargs: _tester_result(tmp_path, "tests_passed"),
    )
    monkeypatch.setattr(orchestrator.workflow_engine, "run_review_loop", lambda *args, **kwargs: None)
    delivery = tmp_path / "final_delivery.md"
    delivery.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(orchestrator.workflow_engine, "run_delivery", lambda *args, **kwargs: delivery)

    result = orchestrator._run_bugfix_flow(task_id, "fix a parsing bug")

    assert result == delivery
    first_fixing = calls.index(("executor", FIXING, 0))
    assert calls[0] == ("planner", PLANNING_DRAFT, 0)
    assert ("planner", PLANNING_PEER_REVIEW, 0) not in calls[:first_fixing]
    assert ("reviewer", PLAN_REVIEW, 0) in calls[:first_fixing]
    assert orchestrator.effective_agent_count(task_id, "planner", PLANNING_DRAFT) == 2

def test_bugfix_flow_uses_tester_result_without_harness_test_gate(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("fix a runtime issue", workflow_type=BUGFIX)
    calls: list[tuple[str, str, int]] = []

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        calls.append((role, phase, round_id))
        return []

    def fake_testing(task_id: str, phase: str, round_id: int, user_prompt: str, **kwargs) -> HarnessTesterResult:
        calls.append(("tester", phase, round_id))
        return _tester_result(tmp_path, "tests_passed")

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)
    monkeypatch.setattr(orchestrator, "run_patch_merge", lambda *args, **kwargs: True)
    monkeypatch.setattr(orchestrator.workflow_engine, "run_testing_until_tester_decision", fake_testing)
    monkeypatch.setattr(orchestrator.workflow_engine, "run_bugfix_planning_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator.workflow_engine, "run_review_loop", lambda *args, **kwargs: None)
    delivery = tmp_path / "final_delivery.md"
    delivery.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(orchestrator.workflow_engine, "run_delivery", lambda *args, **kwargs: delivery)

    orchestrator._run_bugfix_flow(task_id, "fix a runtime issue")

    assert calls[:2] == [
        ("executor", FIXING, 0),
        ("tester", TESTING, 0),
    ]

def test_orchestrator_feature_change_flow_completes(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("add a feature")

    final_delivery = orchestrator.run_task(task_id, workflow_type="feature_change")

    phases = [phase["phase_type"] for phase in orchestrator.repository.list_phases(task_id)]
    assert phases[0] == "PLANNING_DRAFT"
    assert "EXECUTION" in phases
    assert "REVIEWING" in phases
    assert "REVIEW_JUDGEMENT" not in phases
    assert FINAL_JUDGEMENT not in phases
    assert final_delivery.exists()


def test_completed_task_can_be_reused_for_followup_project_workflow(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("Build a small app", workflow_type=NEW_PROJECT)

    first_delivery = orchestrator.run_task(task_id, workflow_type=NEW_PROJECT)
    second_delivery = orchestrator.run_task(
        task_id,
        workflow_type=FEATURE_CHANGE,
        user_prompt_override="Add CSV export to the existing app",
    )

    task = orchestrator.repository.get_task(task_id)
    assert task["status"] == "COMPLETED"
    assert "Follow-up request:\nAdd CSV export to the existing app" in task["user_prompt"]
    assert first_delivery.exists()
    assert second_delivery.exists()
    assert len({phase["task_id"] for phase in orchestrator.repository.list_phases(task_id)}) == 1

def test_source_repo_is_used_only_for_existing_project_workflows(tmp_path: Path) -> None:
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    config = _config(tmp_path)
    config["system"]["source_repo"] = str(source_repo)
    orchestrator = Orchestrator(config)

    orchestrator._active_workflow_type = FEATURE_CHANGE
    assert orchestrator._source_repo_for_workspace() == source_repo.resolve()

    orchestrator._active_workflow_type = NEW_PROJECT
    assert orchestrator._source_repo_for_workspace() is None

def test_project_context_source_repo_overrides_configured_source_repo(tmp_path: Path) -> None:
    configured_repo = tmp_path / "configured"
    configured_repo.mkdir()
    historical_source = tmp_path / "deliver" / "project-12345678" / "source"
    historical_source.mkdir(parents=True)
    config = _config(tmp_path)
    config["system"]["source_repo"] = str(configured_repo)
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("fix the previous delivery", workflow_type=BUGFIX)
    orchestrator.attach_project_context(task_id, f"Historical success_path: {historical_source.parent}\n")

    orchestrator._active_task_id = task_id
    orchestrator._active_workflow_type = BUGFIX
    try:
        assert orchestrator._source_repo_for_workspace() == historical_source.resolve()
        metadata = orchestrator._repo_context_metadata(task_id, "executor", FIXING)
    finally:
        orchestrator._active_task_id = None
        orchestrator._active_workflow_type = None

    assert metadata["repository_source_type"] == "project_context_source_repo"
    assert "repository_source_path" not in metadata
    assert "original source path is intentionally hidden" in metadata["repository_source_note"]
