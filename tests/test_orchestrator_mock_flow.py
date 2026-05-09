from __future__ import annotations

import json
import sys
import uuid
import re
from pathlib import Path
from concurrent.futures import wait as real_wait

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


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
        },
        "agent_backend": {
            "default": "mock",
            "planner": "mock",
            "executor": "mock",
            "tester": "mock",
            "reviewer": "mock",
            "judge": "mock",
            "communicator": "mock",
        },
        "roles": {
            "planner": {"count": 2},
            "executor": {"count": 2},
            "tester": {"count": 2},
            "reviewer": {"count": 2},
            "judge": {"count": 1},
            "communicator": {"count": 1},
        },
        "limits": {
            "max_planning_rounds": 3,
            "max_test_fix_rounds": 5,
            "max_review_rounds": 3,
            "max_agent_retry": 2,
        },
        "timeouts": {
            "planner": 5,
            "executor": 5,
            "tester": 5,
            "reviewer": 5,
            "judge": 5,
            "communicator": 5,
        },
        "policy": {
            "different_roles_can_run_concurrently": False,
            "same_role_can_run_concurrently": True,
            "require_judge_final_approval": True,
            "allow_medium_bug_delivery": False,
            "require_all_tests_pass": True,
        },
    }


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


def test_orchestrator_bugfix_flow_uses_persisted_workflow_and_runs_review(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("fix a failing command", workflow_type=BUGFIX)

    final_delivery = orchestrator.run_task(task_id)

    phases = [phase["phase_type"] for phase in orchestrator.repository.list_phases(task_id)]
    assert "PLANNING_DRAFT" not in phases
    assert "FIXING" in phases
    assert "TESTING" in phases
    assert "REVIEWING" in phases
    assert final_delivery.exists()


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
    monkeypatch.setattr(orchestrator, "_run_patch_merge", lambda task_id, round_id, user_prompt: False)

    try:
        orchestrator.run_task(task_id, workflow_type=BUGFIX)
    except orchestrator_module.TaskFailedError:
        pass

    assert called_rounds == [2, 3]


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
    monkeypatch.setattr(orchestrator, "_run_patch_merge", fake_patch_merge)

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

    monkeypatch.setattr(orchestrator, "_run_patch_merge", fake_patch_merge)
    monkeypatch.setattr(orchestrator, "_run_harness_test_gate", fake_test_gate)
    monkeypatch.setattr(orchestrator, "_run_judge_phase", lambda *args, **kwargs: {"decision": "pass"})
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
    monkeypatch.setattr(orchestrator, "_run_patch_merge", fake_patch_merge)
    monkeypatch.setattr(orchestrator, "_run_harness_test_gate", lambda *args, **kwargs: True)
    monkeypatch.setattr(orchestrator, "_run_judge_phase", lambda *args, **kwargs: {"decision": "pass"})
    monkeypatch.setattr(orchestrator.judge, "is_test_pass", lambda decision: True)

    orchestrator._run_execution_test_loop(task_id, "build a project")

    assert called[:2] == [(FIXING, 8), (TESTING, 8)]
    assert validation_rounds == [8]


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
    monkeypatch.setattr(orchestrator, "_run_patch_merge", lambda *args, **kwargs: True)
    monkeypatch.setattr(orchestrator, "_run_harness_test_gate", lambda *args, **kwargs: True)
    monkeypatch.setattr(orchestrator, "_run_judge_phase", lambda *args, **kwargs: {"decision": "pass"})
    monkeypatch.setattr(orchestrator.judge, "is_test_pass", lambda decision: len(fix_rounds) >= 7)
    monkeypatch.setattr(orchestrator, "_run_review_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_run_final_judgement", lambda *args, **kwargs: None)
    delivery = tmp_path / "final_delivery.md"
    delivery.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_run_delivery", lambda *args, **kwargs: delivery)

    result = orchestrator._run_bugfix_flow(task_id, "fix a failing command")

    assert result == delivery
    assert fix_rounds == list(range(7))


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
    monkeypatch.setattr(orchestrator, "_run_patch_merge", lambda *args, **kwargs: True)
    monkeypatch.setattr(orchestrator, "_run_harness_test_gate", lambda task_id, round_id: tested_rounds.append(round_id) or True)
    monkeypatch.setattr(orchestrator, "_run_judge_phase", fake_judge_phase)
    monkeypatch.setattr(orchestrator.judge, "is_test_pass", lambda decision: decision.get("decision") == "pass")

    orchestrator._run_execution_test_loop(task_id, "build a project")

    assert fix_rounds == [1, 2]
    assert tested_rounds == [0, 1, 2]
    assert any(event.event_type == "test_fix_round_limit_reached" and "已达最大修复轮次(1)" in (event.message or "") for event in events)


def test_orchestrator_feature_change_flow_completes(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("add a feature")

    final_delivery = orchestrator.run_task(task_id, workflow_type="feature_change")

    phases = [phase["phase_type"] for phase in orchestrator.repository.list_phases(task_id)]
    assert phases[0] == "PLANNING_DRAFT"
    assert "EXECUTION" in phases
    assert "REVIEWING" in phases
    assert final_delivery.exists()


def test_regression_testing_runs_harness_test_gate_before_judgement(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review fix", workflow_type=BUGFIX)
    gate_rounds: list[int] = []

    monkeypatch.setattr(orchestrator, "run_role_phase", lambda *args, **kwargs: [])

    def fake_test_gate(task_id: str, round_id: int) -> bool:
        gate_rounds.append(round_id)
        return True

    monkeypatch.setattr(orchestrator, "_run_harness_test_gate", fake_test_gate)
    monkeypatch.setattr(orchestrator, "_run_judge_phase", lambda *args, **kwargs: {"decision": "pass", "tests_passed": True})

    orchestrator._run_regression_test_fix_loop(task_id, "review fix", review_round_id=1, merge_ok=True)

    assert gate_rounds == [5]


def test_planning_block_retries_until_plan_review_approves(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("add a feature")
    review_rounds: list[int] = []

    real_run_role_phase = orchestrator.run_role_phase

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        results = real_run_role_phase(role, phase, round_id, required_outputs, user_prompt, **kwargs)
        if phase == PLAN_REVIEW:
            review_rounds.append(round_id)
            review_report = next(ref.path for result in results for ref in result.artifacts if ref.artifact_type == "review_report.md")
            decision_code = 1 if round_id == 0 else 0
            review_report.write_text(
                f"artifact_result_code: 0\n\n# Review Report\n\nreview_decision_code: {decision_code}\n",
                encoding="utf-8",
            )
        return results

    monkeypatch.setattr(orchestrator, "_run_judge_phase", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planning should not run judge")))
    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)

    orchestrator._run_planning_block(task_id, "add a feature")

    planning_phases = [
        phase
        for phase in orchestrator.repository.list_phases(task_id)
        if phase["phase_type"] in {"PLANNING_DRAFT", "PLANNING_REVISION"}
    ]
    assert review_rounds == [0, 1]
    assert len(planning_phases) == 2
    assert [phase["phase_type"] for phase in planning_phases] == ["PLANNING_DRAFT", "PLANNING_REVISION"]


def test_planning_block_runs_peer_review_loop_then_plan_review(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 2
    config["limits"]["max_planning_rounds"] = 1
    config["limits"]["planning_peer_review_loops"] = 2
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("build peer reviewed plan")

    monkeypatch.setattr(orchestrator, "_run_judge_phase", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planning should not run judge")))

    orchestrator._run_planning_block(task_id, "build peer reviewed plan")

    phases = [(phase["phase_type"], phase["role"], phase["round_id"]) for phase in orchestrator.repository.list_phases(task_id)]
    assert ("PLANNING_DRAFT", "planner", 0) in phases
    assert ("PLANNING_PEER_REVIEW", "planner", 0) in phases
    assert ("PLANNING_REVISION", "planner", 1) in phases
    assert ("PLANNING_PEER_REVIEW", "planner", 1) in phases
    assert ("PLAN_REVIEW", "reviewer", 1) in phases
    assert ("PLAN_JUDGEMENT", "judge", 1) not in phases
    assert orchestrator.repository.list_artifacts(task_id, "peer_review.md")
    assert orchestrator.repository.list_artifacts(task_id, "review_report.md")
    assert orchestrator.repository.list_artifacts(task_id, "selected_plan.md")

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "executor-input", "executor", "EXECUTION")
    manifest = staged[0].read_text(encoding="utf-8")
    assert "selected_plan.md" in manifest
    assert "review_report.md" not in manifest
    assert "planner-1_plan.md" not in manifest
    assert "planner-2_plan.md" not in manifest


def test_plan_review_rejection_enters_planner_fix_review_loop_without_peer_review(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 2
    config["limits"]["max_planning_rounds"] = 3
    config["limits"]["planning_peer_review_loops"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("fix merged planning feedback")
    review_rounds: list[int] = []
    real_run_role_phase = orchestrator.run_role_phase

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        results = real_run_role_phase(role, phase, round_id, required_outputs, user_prompt, **kwargs)
        if phase == PLAN_REVIEW:
            review_rounds.append(round_id)
            review_report = next(ref.path for result in results for ref in result.artifacts if ref.artifact_type == "review_report.md")
            decision_code = 1 if round_id == 0 else 0
            review_report.write_text(
                f"artifact_result_code: 0\n\n# Review Report\n\nreview_decision_code: {decision_code}\n",
                encoding="utf-8",
            )
        return results

    monkeypatch.setattr(orchestrator, "_run_judge_phase", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planning should not run judge")))
    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)

    orchestrator._run_planning_block(task_id, "fix merged planning feedback")

    phases = [(phase["phase_type"], phase["role"], phase["round_id"]) for phase in orchestrator.repository.list_phases(task_id)]
    assert review_rounds == [0, 1]
    assert ("PLANNING_DRAFT", "planner", 0) in phases
    assert ("PLANNING_PEER_REVIEW", "planner", 0) in phases
    assert ("PLANNING_REVISION", "planner", 1) in phases
    assert ("PLAN_REVIEW", "reviewer", 1) in phases
    assert ("PLANNING_PEER_REVIEW", "planner", 1) not in phases


def test_planner_retry_can_see_previous_planning_artifacts(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("revise plan")
    previous_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    current_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 1)
    current_plan = tmp_path / "current-plan.md"
    current_plan.write_text("current", encoding="utf-8")
    for artifact_name in ("plan.md", "risk.md", "todo_breakdown.md"):
        path = tmp_path / artifact_name
        path.write_text(artifact_name, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=previous_phase_id,
                role="planner",
                agent_id="planner-1",
                artifact_type=artifact_name,
                path=path,
                version=1,
                hash="hash",
            )
        )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=current_phase_id,
            role="planner",
            agent_id="planner-1",
            artifact_type="plan.md",
            path=current_plan,
            version=2,
            hash="hash",
        )
    )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "input",
        "planner",
        "PLANNING_DRAFT",
        exclude_phase_id=current_phase_id,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "risk.md" in manifest
    assert "todo_breakdown.md" in manifest
    assert "current-plan.md" not in manifest


def test_plan_review_only_receives_current_planning_round(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review latest plan")
    old_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    current_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_REVISION, "planner", 1)
    for phase_id, round_label, version in (
        (old_phase_id, "old", 1),
        (current_phase_id, "current", 2),
    ):
        for artifact_name in ("plan.md", "risk.md", "todo_breakdown.md", "peer_review.md"):
            path = tmp_path / f"{round_label}-{artifact_name}"
            path.write_text(round_label, encoding="utf-8")
            orchestrator.repository.create_artifact(
                ArtifactRef(
                    artifact_id=str(uuid.uuid4()),
                    task_id=task_id,
                    phase_id=phase_id,
                    role="planner",
                    agent_id="planner-1",
                    artifact_type=artifact_name,
                    path=path,
                    version=version,
                    hash="hash",
                )
            )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "plan-review-input",
        "reviewer",
        PLAN_REVIEW,
        round_id=1,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "current-plan.md" in manifest
    assert "current-peer_review.md" in manifest
    assert "old-plan.md" not in manifest
    assert "old-peer_review.md" not in manifest


def test_artifact_visibility_rule_table_covers_role_phases() -> None:
    covered = {(rule.target_role, rule.target_phase) for rule in orchestrator_module.ARTIFACT_VISIBILITY_RULES}
    intentionally_empty_inputs = {
        ("executor", "MISC_RESPONSE"),
        ("tester", TESTING),
        ("tester", REGRESSION_TESTING),
    }
    role_phases = {
        ("planner", PLANNING_DRAFT),
        ("planner", PLANNING_PEER_REVIEW),
        ("planner", PLANNING_REVISION),
        ("executor", EXECUTION),
        ("executor", PATCH_MERGE),
        ("executor", "MISC_RESPONSE"),
        ("executor", FIXING),
        ("executor", REVIEW_FIXING),
        ("tester", TESTING),
        ("tester", REGRESSION_TESTING),
        ("reviewer", PLAN_REVIEW),
        ("reviewer", REVIEWING),
        ("judge", PLAN_JUDGEMENT),
        ("judge", TEST_JUDGEMENT),
        ("judge", REVIEW_JUDGEMENT),
        ("judge", FINAL_JUDGEMENT),
        ("communicator", DELIVERY),
    }

    assert role_phases <= covered | intentionally_empty_inputs


def test_planner_peer_review_receives_only_current_round_other_planners(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("peer review latest plans")
    old_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    old_peer_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_PEER_REVIEW, "planner", 0)
    current_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_REVISION, "planner", 1)
    current_peer_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_PEER_REVIEW, "planner", 1)

    for phase_id, agent_id, label, artifact_names in (
        (old_phase_id, "planner-1", "old-self", ("plan.md", "risk.md", "todo_breakdown.md", "assumptions.md")),
        (old_peer_phase_id, "planner-2", "old-peer", ("peer_review.md",)),
        (current_phase_id, "planner-1", "current-self", ("plan.md", "risk.md", "todo_breakdown.md", "assumptions.md")),
        (current_phase_id, "planner-2", "current-other", ("plan.md", "risk.md", "todo_breakdown.md", "assumptions.md")),
    ):
        for artifact_name in artifact_names:
            path = tmp_path / f"{label}-{artifact_name}"
            path.write_text(label, encoding="utf-8")
            orchestrator.repository.create_artifact(
                ArtifactRef(
                    artifact_id=str(uuid.uuid4()),
                    task_id=task_id,
                    phase_id=phase_id,
                    role="planner",
                    agent_id=agent_id,
                    artifact_type=artifact_name,
                    path=path,
                    version=1,
                    hash="hash",
                )
            )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "peer-review-input",
        "planner",
        PLANNING_PEER_REVIEW,
        exclude_phase_id=current_peer_phase_id,
        round_id=1,
        current_agent_id="planner-1",
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "current-other-plan.md" in manifest
    assert "current-other-risk.md" in manifest
    assert "current-other-todo_breakdown.md" in manifest
    assert "current-other-assumptions.md" in manifest
    assert "current-self-plan.md" not in manifest
    assert "old-self-plan.md" not in manifest
    assert "old-peer-peer_review.md" not in manifest


def test_planner_revision_after_plan_review_rejection_reads_only_reviewer_feedback(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("revise rejected merged plan")
    planner_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    peer_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_PEER_REVIEW", "planner", 0)
    review_phase_id = orchestrator.repository.create_phase(task_id, PLAN_REVIEW, "reviewer", 0)
    planner_artifacts = [
        (planner_phase_id, "plan.md"),
        (planner_phase_id, "risk.md"),
        (planner_phase_id, "todo_breakdown.md"),
        (peer_phase_id, "peer_review.md"),
    ]
    for phase_id, artifact_name in planner_artifacts:
        path = tmp_path / artifact_name
        path.write_text(artifact_name, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role="planner",
                agent_id="planner-1",
                artifact_type=artifact_name,
                path=path,
                version=1,
                hash="hash",
            )
        )
    review_report = tmp_path / "review_report.md"
    review_report.write_text(
        "artifact_result_code: 0\n\n# Review Report\n\nreview_decision_code: 1\nFix the missing acceptance criteria.\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=review_phase_id,
            role="reviewer",
            agent_id="reviewer-1",
            artifact_type="review_report.md",
            path=review_report,
            version=1,
            hash="hash",
        )
    )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "planner-revision-input",
        "planner",
        PLANNING_REVISION,
        round_id=1,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "review_report.md" in manifest
    assert "plan.md" not in manifest
    assert "risk.md" not in manifest
    assert "todo_breakdown.md" not in manifest
    assert "peer_review.md" not in manifest


def test_orchestrator_misc_flow_uses_executor_response_only(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("what does this dashboard mean?")

    response = orchestrator.run_task(task_id, workflow_type="misc")

    phases = [phase["phase_type"] for phase in orchestrator.repository.list_phases(task_id)]
    executor_runs = [run for run in orchestrator.repository.list_agent_runs(task_id) if run["role"] == "executor"]
    assert phases == ["MISC_RESPONSE"]
    assert len(executor_runs) == 1
    assert response.exists()
    assert response.name == "response.md"
    assert orchestrator.repository.list_artifacts(task_id, "response.md")


def test_orchestrator_emits_progress_events(tmp_path: Path) -> None:
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(_config(tmp_path), progress_callback=events.append)
    task_id = orchestrator.create_task("implement a simple task")

    orchestrator.run_task(task_id)

    event_types = [event.event_type for event in events]
    assert "task_created" in event_types
    assert "phase_started" in event_types
    assert "agent_completed" in event_types
    assert event_types[-1] == "task_completed"
    completed_agent = next(event for event in events if event.event_type == "agent_completed")
    completed_phase = next(event for event in events if event.event_type == "phase_completed")
    assert "elapsed_seconds" in completed_agent.data
    assert "elapsed_seconds" in completed_phase.data
    assert completed_agent.data["elapsed_seconds"] >= 0
    assert completed_phase.data["elapsed_seconds"] >= 0


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


def test_failed_phase_with_completed_agent_runs_is_recovered_on_resume(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("recover old concurrent planner phase")
    phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    for agent_id in ("planner-1", "planner-2"):
        run_id = orchestrator.repository.create_agent_run(task_id, phase_id, "planner", agent_id, 0)
        for artifact_type in required_outputs_for("planner", "PLANNING_DRAFT"):
            path = tmp_path / f"{agent_id}-{artifact_type}"
            content = "return_code: 0\n" if artifact_type == "delivery.md" else artifact_type
            path.write_text(content, encoding="utf-8")
            orchestrator.repository.create_artifact(
                ArtifactRef(
                    artifact_id=str(uuid.uuid4()),
                    task_id=task_id,
                    phase_id=phase_id,
                    role="planner",
                    agent_id=agent_id,
                    artifact_type=artifact_type,
                    path=path,
                    version=1,
                    hash="hash",
                )
            )
        orchestrator.repository.update_agent_run_status(run_id, "COMPLETED")
    orchestrator.repository.update_phase_status(phase_id, "FAILED")

    results = orchestrator.run_role_phase(
        "planner",
        PLANNING_DRAFT,
        0,
        required_outputs_for("planner", PLANNING_DRAFT),
        "recover old concurrent planner phase",
    )

    assert {result.agent_id for result in results} == {"planner-1", "planner-2"}
    assert orchestrator.repository.list_phases(task_id)[0]["status"] == "COMPLETED"


def test_failed_phase_is_not_recovered_when_required_artifacts_are_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("rerun incomplete old phase")
    phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    run_id = orchestrator.repository.create_agent_run(task_id, phase_id, "planner", "planner-1", 0)
    delivery = tmp_path / "delivery.md"
    delivery.write_text("return_code: 0\n", encoding="utf-8")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=phase_id,
            role="planner",
            agent_id="planner-1",
            artifact_type="delivery.md",
            path=delivery,
            version=1,
            hash="hash",
        )
    )
    orchestrator.repository.update_agent_run_status(run_id, "COMPLETED")
    orchestrator.repository.update_phase_status(phase_id, "FAILED")

    orchestrator.run_role_phase(
        "planner",
        PLANNING_DRAFT,
        0,
        required_outputs_for("planner", PLANNING_DRAFT),
        "rerun incomplete old phase",
    )

    phases = orchestrator.repository.list_phases(task_id)
    assert len(phases) == 2
    assert phases[-1]["status"] == "COMPLETED"


def test_checkpoint_resume_prefers_latest_recoverable_phase(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("prefer latest checkpoint")
    old_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    old_run_id = orchestrator.repository.create_agent_run(task_id, old_phase_id, "planner", "planner-1", 0)
    old_delivery = tmp_path / "old-delivery.md"
    old_delivery.write_text("return_code: 0\n", encoding="utf-8")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=old_phase_id,
            role="planner",
            agent_id="planner-1",
            artifact_type="delivery.md",
            path=old_delivery,
            version=1,
            hash="hash",
        )
    )
    orchestrator.repository.update_agent_run_status(old_run_id, "COMPLETED")
    orchestrator.repository.update_phase_status(old_phase_id, "FAILED")

    latest_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    latest_run_id = orchestrator.repository.create_agent_run(task_id, latest_phase_id, "planner", "planner-1", 0)
    for artifact_type in required_outputs_for("planner", PLANNING_DRAFT):
        path = tmp_path / f"latest-{artifact_type}"
        path.write_text("return_code: 0\n" if artifact_type == "delivery.md" else artifact_type, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=latest_phase_id,
                role="planner",
                agent_id="planner-1",
                artifact_type=artifact_type,
                path=path,
                version=2,
                hash="hash",
            )
        )
    orchestrator.repository.update_agent_run_status(latest_run_id, "COMPLETED")
    orchestrator.repository.update_phase_status(latest_phase_id, "COMPLETED")

    results = orchestrator.run_role_phase(
        "planner",
        PLANNING_DRAFT,
        0,
        required_outputs_for("planner", PLANNING_DRAFT),
        "prefer latest checkpoint",
    )

    assert [result.phase_id for result in results] == [latest_phase_id]


def test_judge_checkpoint_resume_parses_existing_decision(tmp_path: Path) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("resume judge")
    orchestrator._active_task_id = task_id
    try:
        phase_id = orchestrator.repository.create_phase(task_id, PLAN_JUDGEMENT, "judge", 0)
        run_id = orchestrator.repository.create_agent_run(task_id, phase_id, "judge", "judge-1", 0)
        artifacts = {
            "decision.json": '{"decision":"approved","changes_required":false}\n',
            "decision_summary.md": "# Decision\napproved\n",
            "delivery.md": "return_code: 0\n",
        }
        for artifact_type, content in artifacts.items():
            path = tmp_path / artifact_type
            path.write_text(content, encoding="utf-8")
            orchestrator.repository.create_artifact(
                ArtifactRef(
                    artifact_id=str(uuid.uuid4()),
                    task_id=task_id,
                    phase_id=phase_id,
                    role="judge",
                    agent_id="judge-1",
                    artifact_type=artifact_type,
                    path=path,
                    version=1,
                    hash="hash",
                )
            )
        orchestrator.repository.update_agent_run_status(run_id, "COMPLETED")
        orchestrator.repository.update_phase_status(phase_id, "COMPLETED")

        decision = orchestrator._run_judge_phase(task_id, PLAN_JUDGEMENT, 0, "resume judge")
    finally:
        orchestrator._active_task_id = None

    assert decision["decision"] == "approved"


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
    assert metadata["repository_source_path"] == str(historical_source.resolve())


def test_patch_validation_uses_project_context_source_repo(tmp_path: Path) -> None:
    configured_repo = tmp_path / "configured"
    configured_repo.mkdir()
    (configured_repo / "app.py").write_text("wrong\n", encoding="utf-8")
    historical_source = tmp_path / "deliver" / "project-12345678" / "source"
    historical_source.mkdir(parents=True)
    (historical_source / "app.py").write_text("old\n", encoding="utf-8")
    config = _config(tmp_path)
    config["system"]["source_repo"] = str(configured_repo)
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("fix the previous delivery", workflow_type=BUGFIX)
    orchestrator.attach_project_context(task_id, f"Historical success_path: {historical_source.parent}\n")
    phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0, status="COMPLETED")
    patch = tmp_path / "merged_patch.diff"
    patch.write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id="merged-patch",
            task_id=task_id,
            phase_id=phase_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="merged_patch.diff",
            path=patch,
            version=1,
            hash="hash",
        )
    )

    assert orchestrator._run_patch_validation(task_id, 0)
    validation_report = Path(orchestrator.repository.list_artifacts(task_id, "patch_validation.md")[-1]["path"]).read_text(encoding="utf-8")
    materialized_report = Path(orchestrator.repository.list_artifacts(task_id, "materialized_repo.md")[-1]["path"]).read_text(encoding="utf-8")
    objective_report = Path(orchestrator.repository.list_artifacts(task_id, "objective_gate.md")[-1]["path"]).read_text(encoding="utf-8")
    materialized_app = orchestrator._latest_materialized_repo(task_id) / "app.py"

    assert f"source_repo: {historical_source.resolve()}" in validation_report
    assert f"source_repo: {historical_source.resolve()}" in materialized_report
    assert "status: pass" in objective_report
    assert "diff_check_status: pass" in materialized_report
    assert materialized_app.read_text(encoding="utf-8") == "new\n"


def test_objective_patch_gate_rejects_sensitive_files(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("do not write secrets", workflow_type=BUGFIX)
    phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0, status="COMPLETED")
    patch = tmp_path / "merged_patch.diff"
    patch.write_text(
        "diff --git a/.env b/.env\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/.env\n"
        "@@ -0,0 +1 @@\n"
        "+TOKEN=secret\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id="sensitive-patch",
            task_id=task_id,
            phase_id=phase_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="merged_patch.diff",
            path=patch,
            version=1,
            hash="hash",
        )
    )

    assert not orchestrator._run_patch_validation(task_id, 0)
    objective_report = Path(orchestrator.repository.list_artifacts(task_id, "objective_gate.md")[-1]["path"]).read_text(encoding="utf-8")

    assert "status: fail" in objective_report
    assert "scope_status: fail" in objective_report
    assert "forbidden sensitive file path: .env" in objective_report


def test_test_judgement_cannot_override_failed_objective_gate(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("judge hard gate", workflow_type=BUGFIX)
    gate = tmp_path / "objective_gate.md"
    gate.write_text(
        "# Objective Gate\n\n"
        "status: fail\n"
        f"task_id: {task_id}\n"
        "round_id: 0\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id="objective-gate",
            task_id=task_id,
            phase_id=None,
            role="orchestrator",
            agent_id="objective-gate",
            artifact_type="objective_gate.md",
            path=gate,
            version=1,
            hash="hash",
        )
    )

    decision = orchestrator._apply_objective_gates_to_judge_decision(
        task_id,
        "TEST_JUDGEMENT",
        0,
        {"decision": "pass", "tests_passed": True, "reason": "LLM says OK"},
    )

    assert decision["decision"] == "fail"
    assert decision["tests_passed"] is False
    assert decision["objective_gate_status"] == "fail"


def test_harness_test_gate_runs_detected_pytest(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("run tests", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_latest_materialized_repo", lambda task_id: repo)

    assert orchestrator._run_harness_test_gate(task_id, 0)
    report = Path(orchestrator.repository.list_artifacts(task_id, "test_gate.md")[-1]["path"]).read_text(encoding="utf-8")

    assert "status: pass" in report
    assert "exit_code: 0" in report


def test_harness_test_gate_records_timeout_failure(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["testing"] = {
        "commands": [f"{sys.executable} -c \"import time; time.sleep(2)\""],
        "timeout_seconds": 1,
    }
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("timeout test", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(orchestrator, "_latest_materialized_repo", lambda task_id: repo)

    assert not orchestrator._run_harness_test_gate(task_id, 0)
    report = Path(orchestrator.repository.list_artifacts(task_id, "test_gate.md")[-1]["path"]).read_text(encoding="utf-8")

    assert "status: fail" in report
    assert "exit_code: timeout" in report


def test_harness_test_gate_does_not_execute_shell_metacharacters(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "shell_injection_marker"
    config = _config(tmp_path)
    config["testing"] = {
        "commands": [f"{sys.executable} -c \"print('ok')\"; touch {marker}"],
        "timeout_seconds": 5,
    }
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("reject shell metacharacters", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(orchestrator, "_latest_materialized_repo", lambda task_id: repo)

    assert orchestrator._run_harness_test_gate(task_id, 0)

    assert not marker.exists()
    report = Path(orchestrator.repository.list_artifacts(task_id, "test_gate.md")[-1]["path"]).read_text(encoding="utf-8")
    assert "status: pass" in report


def test_harness_test_gate_uses_compileall_for_python_repo_without_tests(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("compile python", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_latest_materialized_repo", lambda task_id: repo)

    assert orchestrator._run_harness_test_gate(task_id, 0)
    report = Path(orchestrator.repository.list_artifacts(task_id, "test_gate.md")[-1]["path"]).read_text(encoding="utf-8")

    assert "compileall -q ." in report
    assert "status: pass" in report


def test_test_judgement_cannot_override_failed_required_test_gate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["testing"] = {"require_commands": True, "commands": []}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("judge test gate", workflow_type=BUGFIX)
    objective_gate = tmp_path / "objective_gate.md"
    objective_gate.write_text(
        "# Objective Gate\n\n"
        "status: pass\n"
        "round_id: 0\n"
        "legal_unified_diff: true\n"
        "scope_status: pass\n"
        "size_status: pass\n"
        "patch_apply_status: pass\n"
        "materialize_status: success\n"
        "diff_check_status: pass\n",
        encoding="utf-8",
    )
    test_gate = tmp_path / "test_gate.md"
    test_gate.write_text("# Harness Test Gate\n\nstatus: skipped\nround_id: 0\n", encoding="utf-8")
    for artifact_type, path in (("objective_gate.md", objective_gate), ("test_gate.md", test_gate)):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=artifact_type,
                task_id=task_id,
                phase_id=None,
                role="orchestrator",
                agent_id="gate",
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    decision = orchestrator._apply_objective_gates_to_judge_decision(
        task_id,
        "TEST_JUDGEMENT",
        0,
        {"decision": "pass", "tests_passed": True},
    )

    assert decision["decision"] == "fail"
    assert decision["test_gate_status"] == "skipped"


def test_test_judgement_cannot_override_failed_test_exit_code(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("judge structured test evidence", workflow_type=BUGFIX)
    objective_gate = tmp_path / "objective_gate.md"
    objective_gate.write_text(
        "# Objective Gate\n\n"
        "status: pass\n"
        "round_id: 0\n"
        "legal_unified_diff: true\n"
        "scope_status: pass\n"
        "size_status: pass\n"
        "patch_apply_status: pass\n"
        "materialize_status: success\n"
        "diff_check_status: pass\n",
        encoding="utf-8",
    )
    test_gate = tmp_path / "test_gate.md"
    test_gate.write_text(
        "# Harness Test Gate\n\n"
        "status: pass\n"
        "round_id: 0\n\n"
        "## Evidence JSON\n\n"
        "```json\n"
        '{"status":"pass","build_exit_code":0,"test_exit_code":1,"commands":[]}\n'
        "```\n",
        encoding="utf-8",
    )
    for artifact_type, path in (("objective_gate.md", objective_gate), ("test_gate.md", test_gate)):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=f"structured-{artifact_type}",
                task_id=task_id,
                phase_id=None,
                role="orchestrator",
                agent_id="gate",
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    decision = orchestrator._apply_objective_gates_to_judge_decision(
        task_id,
        "TEST_JUDGEMENT",
        0,
        {"decision": "pass", "tests_passed": True},
    )

    assert decision["decision"] == "fail"
    assert "test_exit_code" in decision["reason"]


def test_patch_validation_selects_merged_patch_from_requested_round(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("fix with multiple rounds", workflow_type=BUGFIX)
    first_phase = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0, status="COMPLETED")
    second_phase = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 1, status="COMPLETED")
    first_patch = tmp_path / "round0.patch"
    second_patch = tmp_path / "round1.patch"
    first_patch.write_text("round0\n", encoding="utf-8")
    second_patch.write_text("round1\n", encoding="utf-8")
    for version, phase_id, path in ((1, first_phase, first_patch), (2, second_phase, second_patch)):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=f"merged-patch-{version}",
                task_id=task_id,
                phase_id=phase_id,
                role="executor",
                agent_id="executor-1",
                artifact_type="merged_patch.diff",
                path=path,
                version=version,
                hash="hash",
            )
        )

    selected = orchestrator._latest_merged_patch_for_round(task_id, 0)

    assert selected
    assert Path(selected["path"]) == first_patch


def test_materialization_diff_check_catches_new_file_whitespace(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("bad whitespace", workflow_type=BUGFIX)
    patch = tmp_path / "bad.patch"
    patch.write_text(
        "diff --git a/bad.py b/bad.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/bad.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1 \n",
        encoding="utf-8",
    )

    result = run_patch_gate(
        patch_path=patch,
        source_repo=None,
        materialized_repo_dir=orchestrator._materialized_repo_dir(task_id, 0),
    )
    report = materialized_repo_markdown(result, task_id, 0)

    assert result.materialized_repo is None
    assert "status: failed" in report
    assert "diff_check_status: fail" in report


def test_materialized_source_applies_modified_file_patch(tmp_path: Path) -> None:
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    (source_repo / "app.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    orchestrator = Orchestrator(_config(tmp_path))
    patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
"""

    files = orchestrator._materialized_files_from_unified_diff(patch, source_repo, include_modified=True)

    assert files[Path("app.py")] == ["one", "TWO", "three"]


def test_failed_materialization_does_not_leave_usable_repo(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("bad patch")
    patch = tmp_path / "bad.patch"
    patch.write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-missing\n"
        "+present\n",
        encoding="utf-8",
    )

    result = run_patch_gate(
        patch_path=patch,
        source_repo=None,
        materialized_repo_dir=orchestrator._materialized_repo_dir(task_id, 0),
    )
    report = materialized_repo_markdown(result, task_id, 0)

    assert result.materialized_repo is None
    assert "status: skipped" in report
    assert not orchestrator._materialized_repo_dir(task_id, 0).exists()
    assert orchestrator._latest_materialized_repo(task_id) is None


def test_latest_materialized_repo_requires_latest_success_artifact_and_marker(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("materialized freshness")
    success_repo = orchestrator._materialized_repo_dir(task_id, 0)
    success_repo.mkdir(parents=True)
    patch = tmp_path / "ok.patch"
    patch.write_text("diff --git a/a.txt b/a.txt\n", encoding="utf-8")
    orchestrator._write_materialized_success_marker(success_repo, task_id, 0, patch)
    success_report = tmp_path / "materialized-success.md"
    success_report.write_text(
        "\n".join(["# Materialized Repository", "", "status: success", f"task_id: {task_id}", "round_id: 0", f"repo_path: {success_repo}", ""]),
        encoding="utf-8",
    )
    failure_report = tmp_path / "materialized-failure.md"
    failure_report.write_text(
        "\n".join(["# Materialized Repository", "", "status: failed", f"task_id: {task_id}", "round_id: 1", "repo_path: none", ""]),
        encoding="utf-8",
    )
    phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0, status="COMPLETED")
    for index, path in enumerate((success_report, failure_report), start=1):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=f"materialized-{index}",
                task_id=task_id,
                phase_id=phase_id,
                role="orchestrator",
                agent_id="patch-materializer",
                artifact_type="materialized_repo.md",
                path=path,
                version=index,
                hash="hash",
            )
        )

    assert orchestrator._latest_materialized_repo(task_id) is None


def test_missing_delivery_md_is_output_invalid_not_file_error(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["executor"]["count"] = 1
    config["limits"]["max_agent_retry"] = 0
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("merge without delivery")

    class MissingDeliveryAdapter:
        def run(self, context):
            context.output_dir.mkdir(parents=True, exist_ok=True)
            context.log_dir.mkdir(parents=True, exist_ok=True)
            (context.output_dir / "merged_patch.diff").write_text("diff --git a/a b/a\n", encoding="utf-8")
            (context.output_dir / "merged_patch_metadata.md").write_text(
                "artifact_result_code: 0\n\npatch_artifact: merged_patch.diff\n", encoding="utf-8"
            )
            (context.output_dir / "merge_report.md").write_text("artifact_result_code: 0\n\nmerged", encoding="utf-8")
            stdout = context.log_dir / "stdout.log"
            stderr = context.log_dir / "stderr.log"
            stdout.write_text("ok", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            return AgentRunResult(
                task_id=context.task_id,
                phase_id=context.phase_id,
                role=context.role,
                agent_id=context.agent_id,
                status="COMPLETED",
                exit_code=0,
                stdout_path=stdout,
                stderr_path=stderr,
            )

    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: MissingDeliveryAdapter())

    try:
        orchestrator.run_role_phase(
            "executor",
            PATCH_MERGE,
            0,
            required_outputs_for("executor", PATCH_MERGE),
            "merge without delivery",
        )
    except Exception as exc:
        assert "Missing required output: delivery.md" in str(exc)
        assert "No such file or directory" not in str(exc)
    else:
        raise AssertionError("Expected missing delivery.md to fail validation")

    runs = orchestrator.repository.list_agent_runs(task_id)
    assert runs[-1]["status"] == "OUTPUT_INVALID"
    assert runs[-1]["error_message"] == "Missing required output: delivery.md"


def test_delivery_contract_review_accepts_format_only_failure(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["executor"]["count"] = 1
    config["limits"]["max_agent_retry"] = 0
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("merge with malformed delivery")

    class MalformedDeliveryAdapter:
        def run(self, context):
            context.output_dir.mkdir(parents=True, exist_ok=True)
            context.log_dir.mkdir(parents=True, exist_ok=True)
            (context.output_dir / "merged_patch.diff").write_text("diff --git a/a b/a\n", encoding="utf-8")
            (context.output_dir / "merged_patch_metadata.md").write_text(
                "artifact_result_code: 0\n\npatch_artifact: merged_patch.diff\n", encoding="utf-8"
            )
            (context.output_dir / "merge_report.md").write_text("artifact_result_code: 0\n\nmerged", encoding="utf-8")
            (context.output_dir / "delivery.md").write_text("status: success\nsummary: completed\n", encoding="utf-8")
            stdout = context.log_dir / "stdout.log"
            stderr = context.log_dir / "stderr.log"
            stdout.write_text("ok", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            return AgentRunResult(
                task_id=context.task_id,
                phase_id=context.phase_id,
                role=context.role,
                agent_id=context.agent_id,
                status="COMPLETED",
                exit_code=0,
                stdout_path=stdout,
                stderr_path=stderr,
            )

    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: MalformedDeliveryAdapter())

    results = orchestrator.run_role_phase(
        "executor",
        PATCH_MERGE,
        0,
        required_outputs_for("executor", PATCH_MERGE),
        "merge with malformed delivery",
    )

    delivery_artifact = next(artifact for artifact in results[0].artifacts if artifact.artifact_type == "delivery.md")
    payload = json.loads(delivery_artifact.path.read_text(encoding="utf-8"))
    runs = orchestrator.repository.list_agent_runs(task_id)

    assert runs[-1]["status"] == "COMPLETED"
    assert payload["return_code"] == 0
    assert payload["contract_review"]["decision"] == "accept"
    assert (Path(payload["contract_review"]["prompt_path"]).parent / "delivery.original.md").exists()


def test_context_window_failure_is_not_retried(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["executor"]["count"] = 1
    config["limits"]["max_agent_retry"] = 2
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    task_id = orchestrator.create_task("large context")

    class ContextWindowAdapter:
        def run(self, context):
            context.log_dir.mkdir(parents=True, exist_ok=True)
            stdout = context.log_dir / "stdout.log"
            stderr = context.log_dir / "stderr.log"
            stdout.write_text("", encoding="utf-8")
            stderr.write_text(
                "ContextWindowExceededError: This model's maximum context length is 200000 tokens.",
                encoding="utf-8",
            )
            return AgentRunResult(
                task_id=context.task_id,
                phase_id=context.phase_id,
                role=context.role,
                agent_id=context.agent_id,
                status="FAILED",
                exit_code=1,
                stdout_path=stdout,
                stderr_path=stderr,
            )

    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: ContextWindowAdapter())

    try:
        orchestrator.run_role_phase(
            "executor",
            PATCH_MERGE,
            0,
            required_outputs_for("executor", PATCH_MERGE),
            "large context",
        )
    except Exception as exc:
        assert "exceeded the model context/request-size budget" in str(exc)
    else:
        raise AssertionError("Expected context-window failure")

    runs = orchestrator.repository.list_agent_runs(task_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "FAILED"
    assert any(event.event_type == "agent_failed" for event in events)
    assert not any(event.event_type == "agent_retryable_failure" for event in events)


def test_current_phase_artifacts_are_excluded_from_agent_inputs(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review work")
    previous_phase_id = orchestrator.repository.create_phase(task_id, "TESTING", "tester", 0)
    current_phase_id = orchestrator.repository.create_phase(task_id, "REVIEWING", "reviewer", 0)
    previous_artifact = tmp_path / "bug_report.md"
    current_artifact = tmp_path / "review_report.md"
    previous_artifact.write_text("previous", encoding="utf-8")
    current_artifact.write_text("current", encoding="utf-8")
    for phase_id, path, agent_id in (
        (previous_phase_id, previous_artifact, "tester-1"),
        (current_phase_id, current_artifact, "reviewer-1"),
    ):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role=agent_id.split("-", 1)[0],
                agent_id=agent_id,
                artifact_type=path.name,
                path=path,
                version=1,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "reviewer", "REVIEWING", exclude_phase_id=current_phase_id)
    manifest = staged[0].read_text(encoding="utf-8")

    assert "bug_report.md" in manifest
    assert "review_report.md" not in manifest


def test_execution_staging_uses_selected_plan_not_raw_planner_outputs(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("implement planned work")
    planner_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    reviewer_phase_id = orchestrator.repository.create_phase(task_id, PLAN_REVIEW, "reviewer", 0)
    artifact_names = ["plan.md", "assumptions.md", "risk.md", "todo_breakdown.md", "peer_review.md"]
    for artifact_name in artifact_names:
        path = tmp_path / artifact_name
        path.write_text(artifact_name, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=planner_phase_id,
                role="planner",
                agent_id="planner-1",
                artifact_type=artifact_name,
                path=path,
                version=1,
                hash="hash",
            )
        )
    for artifact_name in ("selected_plan.md", "review_report.md"):
        path = tmp_path / artifact_name
        path.write_text(artifact_name, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=reviewer_phase_id,
                role="reviewer",
                agent_id="reviewer-1",
                artifact_type=artifact_name,
                path=path,
                version=1,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "executor", "EXECUTION")
    manifest = staged[0].read_text(encoding="utf-8")

    assert "selected_plan.md" in manifest
    assert "review_report.md" not in manifest
    for artifact_name in artifact_names:
        assert f"planner-1_{artifact_name}" not in manifest


def test_test_judgement_sees_only_current_round_test_evidence(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("judge current test evidence")
    old_merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0)
    old_test_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 0)
    current_merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 1)
    current_test_phase_id = orchestrator.repository.create_phase(task_id, REGRESSION_TESTING, "tester", 1)
    current_judge_phase_id = orchestrator.repository.create_phase(task_id, TEST_JUDGEMENT, "judge", 1)

    artifact_rows = [
        ("merged_patch_metadata.md", old_merge_phase_id, "executor", "old-merged-metadata.md", "executor-1"),
        ("bug_report.md", old_test_phase_id, "tester", "old-bug-report.md", "tester-1"),
        ("merged_patch_metadata.md", current_merge_phase_id, "executor", "current-merged-metadata.md", "executor-1"),
        ("merged_patch.diff", current_merge_phase_id, "executor", "current-merged.patch", "executor-1"),
        ("merged_patch_original.diff", current_merge_phase_id, "executor", "current-original.patch", "executor-1"),
        ("merge_report.md", current_merge_phase_id, "executor", "current-merge-report.md", "executor-1"),
        ("delivery.md", current_merge_phase_id, "executor", "executor-delivery.md", "executor-1"),
        ("self_check.md", current_merge_phase_id, "executor", "current-self-check.md", "executor-1"),
        ("fix_schedule.md", current_merge_phase_id, "executor", "current-fix-schedule.md", "executor-1"),
        ("fix_notes.md", current_merge_phase_id, "executor", "current-fix-notes.md", "executor-1"),
        ("bug_report.md", current_test_phase_id, "tester", "current-bug-report.md", "tester-1"),
        ("delivery.md", current_test_phase_id, "tester", "tester-delivery.md", "tester-1"),
    ]
    for artifact_type, phase_id, role, filename, agent_id in artifact_rows:
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role=role,
                agent_id=agent_id,
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    for round_id in (0, 1):
        for artifact_type in ("test_gate.md", "objective_gate.md", "patch_validation.md", "materialized_repo.md"):
            filename = f"judge-round-{round_id}-{artifact_type}"
            path = tmp_path / filename
            path.write_text(f"# {artifact_type}\n\nround_id: {round_id}\n", encoding="utf-8")
            orchestrator.repository.create_artifact(
                ArtifactRef(
                    artifact_id=str(uuid.uuid4()),
                    task_id=task_id,
                    phase_id=None,
                    role="orchestrator",
                    agent_id="orchestrator",
                    artifact_type=artifact_type,
                    path=path,
                    version=round_id + 1,
                    hash="hash",
                )
            )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "input",
        "judge",
        TEST_JUDGEMENT,
        exclude_phase_id=current_judge_phase_id,
        round_id=1,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "current-bug-report.md" in manifest
    assert "judge-round-1-test_gate.md" in manifest
    assert "judge-round-1-objective_gate.md" in manifest

    assert "current-merged-metadata.md" not in manifest
    assert "current-merged.patch" not in manifest
    assert "current-original.patch" not in manifest
    assert "current-merge-report.md" not in manifest
    assert "executor-delivery.md" not in manifest
    assert "current-self-check.md" not in manifest
    assert "current-fix-schedule.md" not in manifest
    assert "current-fix-notes.md" not in manifest
    assert "tester-delivery.md" not in manifest
    assert "old-merged-metadata.md" not in manifest
    assert "old-bug-report.md" not in manifest
    assert "judge-round-0-test_gate.md" not in manifest
    assert "judge-round-0-objective_gate.md" not in manifest
    assert "judge-round-1-patch_validation.md" not in manifest
    assert "judge-round-1-materialized_repo.md" not in manifest
    assert "judge-round-0-patch_validation.md" not in manifest
    assert "judge-round-0-materialized_repo.md" not in manifest


def test_review_judgement_sees_latest_review_and_validation_evidence(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("judge latest review evidence")
    old_merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 1)
    old_test_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 1)
    latest_merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 2)
    latest_test_phase_id = orchestrator.repository.create_phase(task_id, REGRESSION_TESTING, "tester", 2)
    review_phase_id = orchestrator.repository.create_phase(task_id, REVIEWING, "reviewer", 0)
    current_judge_phase_id = orchestrator.repository.create_phase(task_id, REVIEW_JUDGEMENT, "judge", 0)

    artifact_rows = [
        ("merged_patch_metadata.md", old_merge_phase_id, "executor", "old-merged-metadata.md", "executor-1"),
        ("bug_report.md", old_test_phase_id, "tester", "old-bug-report.md", "tester-1"),
        ("merged_patch_metadata.md", latest_merge_phase_id, "executor", "latest-merged-metadata.md", "executor-1"),
        ("bug_report.md", latest_test_phase_id, "tester", "latest-bug-report.md", "tester-1"),
        ("review_report.md", review_phase_id, "reviewer", "current-review-report.md", "reviewer-1"),
    ]
    for artifact_type, phase_id, role, filename, agent_id in artifact_rows:
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role=role,
                agent_id=agent_id,
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    for round_id in (1, 2):
        for artifact_type in ("test_gate.md", "objective_gate.md", "patch_validation.md", "materialized_repo.md"):
            filename = f"review-judge-round-{round_id}-{artifact_type}"
            path = tmp_path / filename
            path.write_text(f"# {artifact_type}\n\nround_id: {round_id}\n", encoding="utf-8")
            orchestrator.repository.create_artifact(
                ArtifactRef(
                    artifact_id=str(uuid.uuid4()),
                    task_id=task_id,
                    phase_id=None,
                    role="orchestrator",
                    agent_id="orchestrator",
                    artifact_type=artifact_type,
                    path=path,
                    version=round_id,
                    hash="hash",
                )
            )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "input",
        "judge",
        REVIEW_JUDGEMENT,
        exclude_phase_id=current_judge_phase_id,
        round_id=0,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "latest-merged-metadata.md" in manifest
    assert "latest-bug-report.md" in manifest
    assert "current-review-report.md" in manifest
    assert "review-judge-round-2-test_gate.md" in manifest
    assert "review-judge-round-2-objective_gate.md" in manifest
    assert "review-judge-round-2-patch_validation.md" in manifest
    assert "review-judge-round-2-materialized_repo.md" in manifest

    assert "old-merged-metadata.md" not in manifest
    assert "old-bug-report.md" not in manifest
    assert "review-judge-round-1-test_gate.md" not in manifest
    assert "review-judge-round-1-objective_gate.md" not in manifest
    assert "review-judge-round-1-patch_validation.md" not in manifest
    assert "review-judge-round-1-materialized_repo.md" not in manifest


def test_final_handoff_stages_latest_evidence_without_candidate_patches(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("finalize latest evidence")
    old_plan_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    latest_plan_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_REVISION, "planner", 2)
    old_exec_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 1)
    latest_exec_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 2)
    old_test_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 1)
    latest_test_phase_id = orchestrator.repository.create_phase(task_id, REGRESSION_TESTING, "tester", 2)
    old_review_phase_id = orchestrator.repository.create_phase(task_id, REVIEWING, "reviewer", 0)
    latest_review_phase_id = orchestrator.repository.create_phase(task_id, REVIEWING, "reviewer", 1)
    old_judge_phase_id = orchestrator.repository.create_phase(task_id, TEST_JUDGEMENT, "judge", 1)
    latest_judge_phase_id = orchestrator.repository.create_phase(task_id, REVIEW_JUDGEMENT, "judge", 2)
    current_final_phase_id = orchestrator.repository.create_phase(task_id, FINAL_JUDGEMENT, "judge", 0)

    artifact_rows = [
        ("plan.md", old_plan_phase_id, "planner", "old-plan.md", "planner-1"),
        ("plan.md", latest_plan_phase_id, "planner", "latest-plan.md", "planner-1"),
        ("merged_patch.diff", old_exec_phase_id, "executor", "old-merged.patch", "executor-1"),
        ("merged_patch.diff", latest_exec_phase_id, "executor", "latest-merged.patch", "executor-1"),
        ("patch.diff", latest_exec_phase_id, "executor", "latest-candidate.patch", "executor-1"),
        ("fix_patch.diff", latest_exec_phase_id, "executor", "latest-fix-candidate.patch", "executor-1"),
        ("bug_report.md", old_test_phase_id, "tester", "old-bug-report.md", "tester-1"),
        ("bug_report.md", latest_test_phase_id, "tester", "latest-bug-report.md", "tester-1"),
        ("review_report.md", old_review_phase_id, "reviewer", "old-review-report.md", "reviewer-1"),
        ("review_report.md", latest_review_phase_id, "reviewer", "latest-review-report.md", "reviewer-1"),
        ("decision.json", old_judge_phase_id, "judge", "old-decision.json", "judge-1"),
        ("decision.json", latest_judge_phase_id, "judge", "latest-decision.json", "judge-1"),
    ]
    for artifact_type, phase_id, role, filename, agent_id in artifact_rows:
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role=role,
                agent_id=agent_id,
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    for round_id in (1, 2):
        path = tmp_path / f"final-round-{round_id}-test_gate.md"
        path.write_text(f"# test_gate.md\n\nround_id: {round_id}\n", encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=None,
                role="orchestrator",
                agent_id="orchestrator",
                artifact_type="test_gate.md",
                path=path,
                version=round_id,
                hash="hash",
            )
        )

    for role, phase in (("judge", FINAL_JUDGEMENT), ("communicator", DELIVERY)):
        staged = orchestrator._stage_input_artifacts(
            task_id,
            tmp_path / f"{role}-input",
            role,
            phase,
            exclude_phase_id=current_final_phase_id if role == "judge" else None,
            round_id=0,
        )
        manifest = staged[0].read_text(encoding="utf-8")

        assert "latest-plan.md" in manifest
        assert "latest-merged.patch" in manifest
        assert "latest-bug-report.md" in manifest
        assert "latest-review-report.md" in manifest
        assert "latest-decision.json" in manifest
        assert "final-round-2-test_gate.md" in manifest

        assert "old-plan.md" not in manifest
        assert "old-merged.patch" not in manifest
        assert "latest-candidate.patch" not in manifest
        assert "latest-fix-candidate.patch" not in manifest
        assert "old-bug-report.md" not in manifest
        assert "old-review-report.md" not in manifest
        assert "old-decision.json" not in manifest
        assert "final-round-1-test_gate.md" not in manifest


def test_tester_receives_no_executor_markdown_artifacts(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("test materialized repository directly")
    execution_phase_id = orchestrator.repository.create_phase(task_id, "EXECUTION", "executor", 0)
    merge_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0)
    for artifact_type, phase_id, agent_id in [
        ("patch.diff", execution_phase_id, "executor-1"),
        ("fix_patch.diff", execution_phase_id, "executor-2"),
        ("merged_patch.diff", merge_phase_id, "executor-1"),
        ("merged_patch_metadata.md", merge_phase_id, "executor-1"),
        ("merge_report.md", merge_phase_id, "executor-1"),
        ("implementation_plan.md", execution_phase_id, "executor-1"),
        ("changed_files.md", execution_phase_id, "executor-1"),
        ("self_check.md", execution_phase_id, "executor-1"),
    ]:
        path = tmp_path / artifact_type
        path.write_text(artifact_type, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role="executor",
                agent_id=agent_id,
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "tester", "TESTING", repo_dir=repo_dir)
    manifest = staged[0].read_text(encoding="utf-8")

    assert staged == [tmp_path / "input" / "manifest.md"]
    assert "## Harness Test Target" in manifest
    assert f"- repository_dir: {repo_dir}" in manifest
    assert "- Treat `repository_dir` as the runnable implementation under test." in manifest
    assert "- app.py" in manifest
    assert "implementation_plan.md" not in manifest
    assert "changed_files.md" not in manifest
    assert "self_check.md" not in manifest
    assert "merge_report.md" not in manifest
    assert "merged_patch.diff" not in manifest
    assert "patch.diff" not in manifest
    assert "fix_patch.diff" not in manifest


def test_tester_does_not_stage_large_authoritative_patch(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("test latest large patch only")
    first_merge_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0)
    second_merge_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 1)
    old_patch = tmp_path / "old_merged.patch"
    old_patch.write_text("old\n" * 20_000, encoding="utf-8")
    latest_patch = tmp_path / "latest_merged.patch"
    latest_patch.write_text("latest\n" * 20_000, encoding="utf-8")
    for version, phase_id, path in ((1, first_merge_phase_id, old_patch), (2, second_merge_phase_id, latest_patch)):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role="executor",
                agent_id="executor-1",
                artifact_type="merged_patch.diff",
                path=path,
                version=version,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "tester", "TESTING", repo_dir=tmp_path / "repo")
    manifest = staged[0].read_text(encoding="utf-8")

    assert staged == [tmp_path / "input" / "manifest.md"]
    assert "## Harness Test Target" in manifest
    assert "merged_patch.diff" not in manifest
    assert "latest_merged.patch" not in manifest
    assert "old_merged.patch" not in manifest


def test_patch_merge_sees_current_round_candidate_and_previous_authoritative_patch_only(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("merge current patch only")
    execution_phase_id = orchestrator.repository.create_phase(task_id, "EXECUTION", "executor", 0)
    old_fix_phase_id = orchestrator.repository.create_phase(task_id, "FIXING", "executor", 1)
    previous_merge_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 1)
    current_fix_phase_id = orchestrator.repository.create_phase(task_id, "FIXING", "executor", 2)
    current_merge_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 2)
    artifact_rows = [
        ("patch.diff", execution_phase_id, "executor", "old-execution.patch"),
        ("fix_patch.diff", old_fix_phase_id, "executor", "old-fix.patch"),
        ("merged_patch.diff", previous_merge_phase_id, "executor", "previous-merged.patch"),
        ("merged_patch_metadata.md", previous_merge_phase_id, "executor", "previous-merged-metadata.md"),
        ("merge_report.md", previous_merge_phase_id, "executor", "previous-merge-report.md"),
        ("fix_patch.diff", current_fix_phase_id, "executor", "current-fix.patch"),
        ("self_check.md", current_fix_phase_id, "executor", "current-self-check.md"),
        ("fix_notes.md", current_fix_phase_id, "executor", "current-fix-notes.md"),
        ("bug_report.md", old_fix_phase_id, "tester", "old-bug-report.md"),
        ("decision.json", old_fix_phase_id, "judge", "old-decision.json"),
        ("review_report.md", old_fix_phase_id, "reviewer", "old-review-report.md"),
        ("plan.md", execution_phase_id, "planner", "old-plan.md"),
    ]
    for artifact_type, phase_id, role, filename in artifact_rows:
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role=role,
                agent_id="executor-1",
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "input",
        "executor",
        "PATCH_MERGE",
        exclude_phase_id=current_merge_phase_id,
        round_id=2,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "current-fix.patch" in manifest
    assert "previous-merged.patch" in manifest
    assert "previous-merged-metadata.md" in manifest
    assert "previous-merge-report.md" not in manifest
    assert "current-self-check.md" not in manifest
    assert "current-fix-notes.md" not in manifest
    assert "old-bug-report.md" not in manifest
    assert "old-decision.json" not in manifest
    assert "old-review-report.md" not in manifest
    assert "old-plan.md" not in manifest
    assert "old-execution.patch" not in manifest
    assert "old-fix.patch" not in manifest


def test_fixing_sees_only_previous_round_failure_evidence(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("fix latest test failure only")
    round0_merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0)
    round0_test_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 0)
    round0_judge_phase_id = orchestrator.repository.create_phase(task_id, TEST_JUDGEMENT, "judge", 0)
    stale_fix_phase_id = orchestrator.repository.create_phase(task_id, FIXING, "executor", 1)
    round1_merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 1)
    round1_test_phase_id = orchestrator.repository.create_phase(task_id, REGRESSION_TESTING, "tester", 1)
    round1_judge_phase_id = orchestrator.repository.create_phase(task_id, REVIEW_JUDGEMENT, "judge", 1)
    current_fix_phase_id = orchestrator.repository.create_phase(task_id, FIXING, "executor", 2)

    artifact_rows = [
        ("merged_patch_metadata.md", round0_merge_phase_id, "executor", "old-merged-metadata.md", "executor-1"),
        ("bug_report.md", round0_test_phase_id, "tester", "old-bug-report.md", "tester-1"),
        ("decision.json", round0_judge_phase_id, "judge", "old-decision.json", "judge-1"),
        ("decision_summary.md", round0_judge_phase_id, "judge", "old-decision-summary.md", "judge-1"),
        ("merged_patch_metadata.md", round1_merge_phase_id, "executor", "latest-merged-metadata.md", "executor-1"),
        ("merged_patch.diff", round1_merge_phase_id, "executor", "latest-merged.patch", "executor-1"),
        ("merge_report.md", round1_merge_phase_id, "executor", "latest-merge-report.md", "executor-1"),
        ("bug_report.md", round1_test_phase_id, "tester", "latest-bug-report.md", "tester-1"),
        ("decision.json", round1_judge_phase_id, "judge", "latest-decision.json", "judge-1"),
        ("decision_summary.md", round1_judge_phase_id, "judge", "latest-decision-summary.md", "judge-1"),
        ("self_check.md", stale_fix_phase_id, "executor", "stale-self-check.md", "executor-1"),
        ("implementation_plan.md", stale_fix_phase_id, "executor", "stale-plan.md", "executor-1"),
        ("changed_files.md", stale_fix_phase_id, "executor", "stale-changed-files.md", "executor-1"),
    ]
    for artifact_type, phase_id, role, filename, agent_id in artifact_rows:
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role=role,
                agent_id=agent_id,
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    for round_id in (0, 1):
        for artifact_type in ("test_gate.md", "objective_gate.md", "patch_validation.md", "materialized_repo.md"):
            filename = f"round-{round_id}-{artifact_type}"
            path = tmp_path / filename
            path.write_text(f"# {artifact_type}\n\nround_id: {round_id}\n", encoding="utf-8")
            orchestrator.repository.create_artifact(
                ArtifactRef(
                    artifact_id=str(uuid.uuid4()),
                    task_id=task_id,
                    phase_id=None,
                    role="orchestrator",
                    agent_id="orchestrator",
                    artifact_type=artifact_type,
                    path=path,
                    version=round_id + 1,
                    hash="hash",
                )
            )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "input",
        "executor",
        FIXING,
        exclude_phase_id=current_fix_phase_id,
        round_id=2,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "latest-merged-metadata.md" in manifest
    assert "latest-bug-report.md" in manifest
    assert "latest-decision.json" in manifest
    assert "latest-decision-summary.md" in manifest
    assert "round-1-test_gate.md" in manifest
    assert "round-1-objective_gate.md" in manifest
    assert "round-1-patch_validation.md" in manifest
    assert "round-1-materialized_repo.md" in manifest

    assert "latest-merged.patch" not in manifest
    assert "latest-merge-report.md" not in manifest
    assert "stale-self-check.md" not in manifest
    assert "stale-plan.md" not in manifest
    assert "stale-changed-files.md" not in manifest
    assert "old-merged-metadata.md" not in manifest
    assert "old-bug-report.md" not in manifest
    assert "old-decision.json" not in manifest
    assert "old-decision-summary.md" not in manifest
    assert "round-0-test_gate.md" not in manifest
    assert "round-0-objective_gate.md" not in manifest
    assert "round-0-patch_validation.md" not in manifest
    assert "round-0-materialized_repo.md" not in manifest


def test_fixing_falls_back_to_latest_visible_test_evidence_when_previous_test_outputs_invalid(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("fix with missing latest tester artifacts")
    round0_test_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 0, status="COMPLETED")
    round0_judge_phase_id = orchestrator.repository.create_phase(task_id, TEST_JUDGEMENT, "judge", 0, status="COMPLETED")
    round1_merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 1, status="COMPLETED")
    round1_test_phase_id = orchestrator.repository.create_phase(task_id, REGRESSION_TESTING, "tester", 1, status=FAILED)
    current_fix_phase_id = orchestrator.repository.create_phase(task_id, FIXING, "executor", 2)
    orchestrator.repository.create_judge_decision(
        task_id,
        round0_judge_phase_id,
        TEST_JUDGEMENT,
        {"phase": TEST_JUDGEMENT, "decision": "fail", "tests_passed": False},
    )

    artifact_rows = [
        ("bug_report.md", round0_test_phase_id, "tester", "round0-bug-report.md", "tester-1"),
        ("review_report.md", round1_test_phase_id, "tester", "round1-non-tester-report.md", "tester-1"),
        ("decision.json", round0_judge_phase_id, "judge", "round0-decision.json", "judge-1"),
        ("decision_summary.md", round0_judge_phase_id, "judge", "round0-decision-summary.md", "judge-1"),
        ("merged_patch_metadata.md", round1_merge_phase_id, "executor", "round1-merged-metadata.md", "executor-1"),
        ("merged_patch.diff", round1_merge_phase_id, "executor", "round1-merged.patch", "executor-1"),
    ]
    for artifact_type, phase_id, role, filename, agent_id in artifact_rows:
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role=role,
                agent_id=agent_id,
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    for round_id in (0, 1):
        for artifact_type in ("test_gate.md", "objective_gate.md", "patch_validation.md", "materialized_repo.md"):
            path = tmp_path / f"round-{round_id}-{artifact_type}"
            path.write_text(f"# {artifact_type}\n\nround_id: {round_id}\n", encoding="utf-8")
            orchestrator.repository.create_artifact(
                ArtifactRef(
                    artifact_id=str(uuid.uuid4()),
                    task_id=task_id,
                    phase_id=round1_merge_phase_id if round_id == 1 else None,
                    role="orchestrator",
                    agent_id="orchestrator",
                    artifact_type=artifact_type,
                    path=path,
                    version=round_id + 1,
                    hash="hash",
                )
            )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "input",
        "executor",
        FIXING,
        exclude_phase_id=current_fix_phase_id,
        round_id=2,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "round0-bug-report.md" in manifest
    assert "round1-non-tester-report.md" not in manifest
    assert "round0-decision.json" in manifest
    assert "round0-decision-summary.md" in manifest
    assert "round1-merged-metadata.md" in manifest
    assert "round1-merged.patch" not in manifest
    assert "failed_test_round_count_before_current: 2" in manifest
    assert "failed_test_round_ids_before_current: 0, 1" in manifest
    assert "latest_visible_complete_test_evidence_round: 0" in manifest
    assert "failed_test_rounds_without_complete_visible_reports: 1" in manifest


def test_invalid_patch_merge_skips_testing_and_enters_fix_round(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("repair invalid patch")
    validation_rounds: list[int] = []

    def fake_patch_validation(task_id: str, round_id: int) -> bool:
        validation_rounds.append(round_id)
        if round_id <= 0:
            return False
        gate = tmp_path / f"objective_gate_round_{round_id}.md"
        gate.write_text(
            "# Objective Gate\n\n"
            "status: pass\n"
            f"task_id: {task_id}\n"
            f"round_id: {round_id}\n"
            "legal_unified_diff: true\n"
            "scope_status: pass\n"
            "size_status: pass\n"
            "patch_apply_status: pass\n"
            "materialize_status: success\n"
            "diff_check_status: pass\n"
            "changed_line_count: 1\n"
            "deleted_file_count: 0\n",
            encoding="utf-8",
        )
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=f"objective-gate-{round_id}",
                task_id=task_id,
                phase_id=None,
                role="orchestrator",
                agent_id="objective-gate",
                artifact_type="objective_gate.md",
                path=gate,
                version=round_id,
                hash="hash",
            )
        )
        return True

    def fake_test_gate(task_id: str, round_id: int) -> bool:
        gate = tmp_path / f"test_gate_round_{round_id}.md"
        gate.write_text("# Harness Test Gate\n\nstatus: pass\nround_id: {round_id}\n".format(round_id=round_id), encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=f"test-gate-{round_id}",
                task_id=task_id,
                phase_id=None,
                role="orchestrator",
                agent_id="test-gate",
                artifact_type="test_gate.md",
                path=gate,
                version=round_id,
                hash="hash",
            )
        )
        return True

    monkeypatch.setattr(orchestrator, "_run_patch_validation", fake_patch_validation)
    monkeypatch.setattr(orchestrator, "_run_harness_test_gate", fake_test_gate)
    monkeypatch.setattr(orchestrator, "_run_judge_phase", lambda *args, **kwargs: {"decision": "pass"})
    monkeypatch.setattr(orchestrator.judge, "is_test_pass", lambda decision: True)

    orchestrator._run_execution_test_loop(task_id, "repair invalid patch")

    phases = [(phase["phase_type"], phase["round_id"]) for phase in orchestrator.repository.list_phases(task_id)]
    assert validation_rounds[:2] == [0, 1]
    assert ("FIXING", 1) in phases
    assert ("TESTING", 0) not in phases
    assert ("TESTING", 1) in phases


def test_final_execution_fix_round_is_tested_before_exhaustion(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["limits"]["max_test_fix_rounds"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("repair invalid patch")
    validation_rounds: list[int] = []
    tested_rounds: list[int] = []

    def fake_patch_validation(task_id: str, round_id: int) -> bool:
        validation_rounds.append(round_id)
        return round_id == 1

    def fake_test_gate(task_id: str, round_id: int) -> bool:
        tested_rounds.append(round_id)
        return True

    monkeypatch.setattr(orchestrator, "_run_patch_validation", fake_patch_validation)
    monkeypatch.setattr(orchestrator, "_run_harness_test_gate", fake_test_gate)
    monkeypatch.setattr(orchestrator, "_run_judge_phase", lambda *args, **kwargs: {"decision": "pass"})
    monkeypatch.setattr(orchestrator.judge, "is_test_pass", lambda decision: True)

    orchestrator._run_execution_test_loop(task_id, "repair invalid patch")

    phases = [(phase["phase_type"], phase["round_id"]) for phase in orchestrator.repository.list_phases(task_id)]
    assert validation_rounds == [0, 1]
    assert tested_rounds == [1]
    assert ("FIXING", 1) in phases
    assert ("TESTING", 1) in phases


def test_staged_input_artifacts_respect_size_budget(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["artifact_input"] = {"max_files": 1, "max_file_bytes": 40, "max_total_bytes": 60}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("review large artifacts")
    tester_phase_id = orchestrator.repository.create_phase(task_id, "TESTING", "tester", 0)
    executor_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0)
    small_self_check = tmp_path / "self_check.md"
    small_self_check.write_text("self", encoding="utf-8")
    large_report = tmp_path / "bug_report.md"
    large_report.write_text("A" * 200, encoding="utf-8")
    for artifact_type, phase_id, role, path in (
        ("self_check.md", executor_phase_id, "executor", small_self_check),
        ("bug_report.md", tester_phase_id, "tester", large_report),
    ):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role=role,
                agent_id=f"{role}-1",
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "reviewer", "REVIEWING")
    manifest = staged[0].read_text(encoding="utf-8")

    assert len(staged) == 2
    assert "truncated: true" in manifest
    assert "skipped: true" in manifest
    assert staged[1].stat().st_size < large_report.stat().st_size


def test_reviewer_receives_large_merged_patch_truncated_not_full(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["artifact_input"] = {"max_files": 5, "max_file_bytes": 262144, "max_total_bytes": 262144}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("review large patch")
    merge_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0)
    patch = tmp_path / "merged.patch"
    patch.write_text("diff line\n" * 20_000, encoding="utf-8")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=merge_phase_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="merged_patch.diff",
            path=patch,
            version=1,
            hash="hash",
        )
    )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "reviewer", "REVIEWING")
    manifest = staged[0].read_text(encoding="utf-8")

    assert len(staged) == 2
    assert "truncated: true" in manifest
    assert staged[1].stat().st_size <= 16_384


def test_delivery_is_published_to_shallow_deliver_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["system"]["deliver_root"] = str(tmp_path / "deliver")
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("Build Weather Tool")

    final_delivery = orchestrator.run_task(task_id)

    merge_phases = [
        phase for phase in orchestrator.repository.list_phases(task_id) if phase["phase_type"] == "PATCH_MERGE"
    ]
    assert merge_phases
    assert {phase["role"] for phase in merge_phases} == {"executor"}
    assert final_delivery == tmp_path / "deliver" / f"build-weather-tool-{task_id[:8]}" / "final_delivery.md"
    assert final_delivery.exists()
    assert (final_delivery.parent / "success_path.md").exists()
    assert (final_delivery.parent / "usage_guide.md").exists()
    assert (final_delivery.parent / "patches" / "final.patch").exists()
    assert (final_delivery.parent / "artifacts" / "merged_patch.diff").exists()
    assert (final_delivery.parent / "artifacts" / "merged_patch_metadata.md").exists()
    assert (final_delivery.parent / "artifacts" / "patch_validation.md").exists()
    assert (final_delivery.parent / "artifacts" / "materialized_repo.md").exists()
    assert (final_delivery.parent / "artifacts" / "merge_report.md").exists()
    assert (final_delivery.parent / "artifacts" / "patch.diff").exists()
    assert (final_delivery.parent / "source" / "mock.txt").read_text(encoding="utf-8") == "mock change\n"
    merged_artifacts = orchestrator.repository.list_artifacts(task_id, "merged_patch.diff")
    validation_artifacts = orchestrator.repository.list_artifacts(task_id, "patch_validation.md")
    materialized_artifacts = orchestrator.repository.list_artifacts(task_id, "materialized_repo.md")
    success_path_artifacts = orchestrator.repository.list_artifacts(task_id, "success_path.md")
    assert merged_artifacts
    assert validation_artifacts
    assert "status: pass" in Path(validation_artifacts[-1]["path"]).read_text(encoding="utf-8")
    assert materialized_artifacts
    materialized_report = Path(materialized_artifacts[-1]["path"]).read_text(encoding="utf-8")
    assert "status: success" in materialized_report
    assert success_path_artifacts
    assert Path(success_path_artifacts[-1]["path"]) == final_delivery.parent / "success_path.md"
    assert (final_delivery.parent / "patches" / "final.patch").read_text(encoding="utf-8") == Path(
        merged_artifacts[-1]["path"]
    ).read_text(encoding="utf-8")
    manifest = (final_delivery.parent / "artifacts_manifest.md").read_text(encoding="utf-8")
    success_path = (final_delivery.parent / "success_path.md").read_text(encoding="utf-8")
    assert f"success_path: {final_delivery.parent}" in manifest
    assert f"success_path: {final_delivery.parent}" in success_path
    assert "patches/final.patch" in manifest
    assert "patch_validation.md" in manifest
    assert "materialized_repo.md" in manifest
    assert "source/mock.txt" in manifest
    tester_run = next(
        run
        for run in orchestrator.repository.list_agent_runs(task_id)
        if run["role"] == "tester" and run["status"] == "COMPLETED"
    )
    tester_phase = next(
        phase for phase in orchestrator.repository.list_phases(task_id) if phase["phase_id"] == tester_run["phase_id"]
    )
    tester_repo = (
        Path(config["system"]["workspace_root"])
        / task_id
        / tester_run["phase_id"]
        / "tester"
        / tester_run["agent_id"]
        / f"round_{tester_phase['round_id']}"
        / f"attempt_{tester_run['retry_count']}"
        / "repo"
    )
    assert (tester_repo / "mock.txt").read_text(encoding="utf-8") == "mock change\n"


def test_delivery_dependency_installer_infers_pytest_dependencies(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    project_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = project_dir / "source"
    (source_dir / "tests").mkdir(parents=True)
    (source_dir / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (source_dir / "src").mkdir()
    (source_dir / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (project_dir / "usage_guide.md").write_text(
        "```bash\npython3 -m pytest --cov=src tests/\n```\n",
        encoding="utf-8",
    )

    written = orchestrator._publish_dependency_installer(project_dir)

    requirements = source_dir / "requirements.txt"
    installer = source_dir / "install_dependencies.sh"
    assert requirements in written
    assert installer in written
    assert requirements.read_text(encoding="utf-8") == "pytest\npytest-cov\n"
    assert "pip install -r requirements.txt" in installer.read_text(encoding="utf-8")
    assert installer.stat().st_mode & 0o111


def test_delivery_dependency_installer_prefers_pyproject_dev_install(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    project_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\n[project.optional-dependencies]\ndev = [\"pytest\"]\n",
        encoding="utf-8",
    )

    written = orchestrator._publish_dependency_installer(project_dir)

    installer = source_dir / "install_dependencies.sh"
    assert written == [installer]
    assert 'pip install -e ".[dev]"' in installer.read_text(encoding="utf-8")


def test_delivery_project_name_uses_ascii_safe_slug(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["system"]["deliver_root"] = str(tmp_path / "deliver")
    orchestrator = Orchestrator(config)
    task_id = "859fe499-d655-455d-933d-34021a4aea67"

    assert orchestrator._slugify_project_name("做个双人对战的象棋游戏") == "project"
    assert orchestrator._slugify_project_name("做个 Chinese Chess Game!") == "chinese-chess-game"
    assert orchestrator._delivery_project_dir(task_id, "做个双人对战的象棋游戏").name == "project-859fe499"


def test_agent_heartbeat_events_are_emitted_for_long_running_agents(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    config["mock"] = {"delay_seconds": 0.05}
    config["heartbeat"] = {"interval_seconds": 0.01}
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    orchestrator.create_task("plan with heartbeat")

    orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, required_outputs_for("planner", PLANNING_DRAFT), "plan with heartbeat")

    heartbeats = [event for event in events if event.event_type == "agent_heartbeat"]
    assert heartbeats
    assert heartbeats[0].role == "planner"
    assert heartbeats[0].status == "RUNNING"
