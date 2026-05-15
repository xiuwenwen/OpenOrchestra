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


def _review_result_payload(decision_code: int) -> dict:
    status = {0: "approved", 1: "changes_required", -1: "blocked"}[decision_code]
    return {
        "schema_version": 1,
        "review_decision_code": decision_code,
        "review_status": status,
        "summary": "plan review verdict",
        "findings": [],
        "required_changes": [] if decision_code == 0 else ["revise selected plan"],
        "environment_check": {
            "attempted": False,
            "status": "not_applicable",
            "commands_run": [],
            "fixable": False,
            "blocking_reason": "",
        },
    }


def _write_review_result(path: Path, decision_code: int) -> None:
    path.write_text(json.dumps(_review_result_payload(decision_code)) + "\n", encoding="utf-8")


def test_existing_project_workflows_cap_planner_agents_at_two(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 4
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("modify existing project", workflow_type=FEATURE_CHANGE)
    orchestrator._active_workflow_type = FEATURE_CHANGE
    try:
        assert orchestrator._effective_agent_count(task_id, "planner", PLANNING_DRAFT) == 2
    finally:
        orchestrator._active_workflow_type = None

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
            review_report = next(ref.path for result in results for ref in result.artifacts if ref.artifact_type == "review_result.json")
            decision_code = 1 if round_id == 0 else 0
            _write_review_result(review_report, decision_code)
        return results

    monkeypatch.setattr(orchestrator, "run_judge_phase", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planning should not run judge")))
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


def test_plan_review_accepts_review_result_json(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("accept json plan review verdict")
    report_path = tmp_path / "review_result.json"
    _write_review_result(report_path, 0)
    report_ref = ArtifactRef(
        artifact_id=str(uuid.uuid4()),
        task_id=task_id,
        phase_id="plan-review-phase",
        role="reviewer",
        agent_id="reviewer-1",
        artifact_type="review_result.json",
        path=report_path,
        version=1,
        hash="hash",
    )
    result = AgentRunResult(task_id, "plan-review-phase", "reviewer", "reviewer-1", "COMPLETED", artifacts=[report_ref])

    assert orchestrator.workflow_engine.plan_review_approved([result])


def test_planning_block_runs_peer_review_loop_then_plan_review(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 2
    config["limits"]["max_planning_rounds"] = 1
    config["limits"]["planning_peer_review_loops"] = 2
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("build peer reviewed plan")

    monkeypatch.setattr(orchestrator, "run_judge_phase", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planning should not run judge")))

    orchestrator._run_planning_block(task_id, "build peer reviewed plan")

    phases = [(phase["phase_type"], phase["role"], phase["round_id"]) for phase in orchestrator.repository.list_phases(task_id)]
    assert ("PLANNING_DRAFT", "planner", 0) in phases
    assert ("PLANNING_PEER_REVIEW", "planner", 0) in phases
    assert ("PLANNING_REVISION", "planner", 1) in phases
    assert ("PLANNING_PEER_REVIEW", "planner", 1) in phases
    assert ("PLAN_REVIEW", "reviewer", 1) in phases
    assert ("PLAN_JUDGEMENT", "judge", 1) not in phases
    assert orchestrator.repository.list_artifacts(task_id, "peer_review_result.json")
    assert orchestrator.repository.list_artifacts(task_id, "review_result.json")
    assert orchestrator.repository.list_artifacts(task_id, "selected_plan.json")

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "executor-input", "executor", "EXECUTION")
    manifest = staged[0].read_text(encoding="utf-8")
    assert "selected_plan.json" in manifest
    assert "review_result.json" not in manifest
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
            review_report = next(ref.path for result in results for ref in result.artifacts if ref.artifact_type == "review_result.json")
            decision_code = 1 if round_id == 0 else 0
            _write_review_result(review_report, decision_code)
        return results

    monkeypatch.setattr(orchestrator, "run_judge_phase", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planning should not run judge")))
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
    for artifact_name in ("plan.md", "risk.md", "todo_breakdown.json"):
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
    assert "todo_breakdown.json" in manifest
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
        for artifact_name in ("plan.md", "risk.md", "todo_breakdown.json", "peer_review_result.json"):
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
    assert "current-peer_review_result.json" in manifest
    assert "old-plan.md" not in manifest
    assert "old-peer_review_result.json" not in manifest

def test_planner_peer_review_receives_only_current_round_other_planners(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("peer review latest plans")
    old_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    old_peer_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_PEER_REVIEW, "planner", 0)
    current_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_REVISION, "planner", 1)
    current_peer_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_PEER_REVIEW, "planner", 1)

    for phase_id, agent_id, label, artifact_names in (
        (old_phase_id, "planner-1", "old-self", ("plan.md", "risk.md", "todo_breakdown.json", "assumptions.md")),
        (old_peer_phase_id, "planner-2", "old-peer", ("peer_review_result.json",)),
        (current_phase_id, "planner-1", "current-self", ("plan.md", "risk.md", "todo_breakdown.json", "assumptions.md")),
        (current_phase_id, "planner-2", "current-other", ("plan.md", "risk.md", "todo_breakdown.json", "assumptions.md")),
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
    assert "current-other-todo_breakdown.json" in manifest
    assert "current-other-assumptions.md" in manifest
    assert "current-self-plan.md" not in manifest
    assert "old-self-plan.md" not in manifest
    assert "old-peer-peer_review_result.json" not in manifest

def test_planner_revision_after_plan_review_rejection_reads_only_reviewer_feedback(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("revise rejected merged plan")
    planner_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    peer_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_PEER_REVIEW", "planner", 0)
    review_phase_id = orchestrator.repository.create_phase(task_id, PLAN_REVIEW, "reviewer", 0)
    planner_artifacts = [
        (planner_phase_id, "plan.md"),
        (planner_phase_id, "risk.md"),
        (planner_phase_id, "todo_breakdown.json"),
        (peer_phase_id, "peer_review_result.json"),
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
    review_report = tmp_path / "review_result.json"
    _write_review_result(review_report, 1)
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=review_phase_id,
            role="reviewer",
            agent_id="reviewer-1",
            artifact_type="review_result.json",
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

    assert "review_result.json" in manifest
    assert "plan.md" not in manifest
    assert "risk.md" not in manifest
    assert "todo_breakdown.json" not in manifest
    assert "peer_review_result.json" not in manifest

def test_planning_revision_only_receives_previous_round_artifacts(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("revise only against prior round")

    round0_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    round0_peer_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_PEER_REVIEW, "planner", 0)
    round0_judge_phase_id = orchestrator.repository.create_phase(task_id, PLAN_JUDGEMENT, "judge", 0)
    round1_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_REVISION, "planner", 1)
    round1_peer_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_PEER_REVIEW, "planner", 1)
    round1_judge_phase_id = orchestrator.repository.create_phase(task_id, PLAN_JUDGEMENT, "judge", 1)
    current_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_REVISION, "planner", 2)

    artifact_rows = [
        (round0_phase_id, "planner", "planner-1", "plan.md", "round0-plan.md"),
        (round0_phase_id, "planner", "planner-1", "risk.md", "round0-risk.md"),
        (round0_peer_phase_id, "planner", "planner-2", "peer_review_result.json", "round0-peer-review.json"),
        (round0_judge_phase_id, "judge", "judge-1", "decision.json", "round0-decision.json"),
        (round1_phase_id, "planner", "planner-1", "plan.md", "round1-plan.md"),
        (round1_phase_id, "planner", "planner-1", "risk.md", "round1-risk.md"),
        (round1_phase_id, "planner", "planner-1", "todo_breakdown.json", "round1-todo.json"),
        (round1_phase_id, "planner", "planner-1", "assumptions.md", "round1-assumptions.md"),
        (round1_peer_phase_id, "planner", "planner-2", "peer_review_result.json", "round1-peer-review.json"),
        (round1_judge_phase_id, "judge", "judge-1", "decision.json", "round1-decision.json"),
    ]
    for phase_id, role, agent_id, artifact_type, filename in artifact_rows:
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

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "planner-revision-round-2-input",
        "planner",
        PLANNING_REVISION,
        exclude_phase_id=current_phase_id,
        round_id=2,
        current_agent_id="planner-1",
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "round1-plan.md" in manifest
    assert "round1-risk.md" in manifest
    assert "round1-todo.json" in manifest
    assert "round1-assumptions.md" in manifest
    assert "round1-peer-review.json" in manifest
    assert "round1-decision.json" in manifest

    assert "round0-plan.md" not in manifest
    assert "round0-risk.md" not in manifest
    assert "round0-peer-review.json" not in manifest
    assert "round0-decision.json" not in manifest

def test_execution_staging_uses_selected_plan_not_raw_planner_outputs(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("implement planned work")
    planner_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    reviewer_phase_id = orchestrator.repository.create_phase(task_id, PLAN_REVIEW, "reviewer", 0)
    artifact_names = ["plan.md", "assumptions.md", "risk.md", "todo_breakdown.json", "peer_review_result.json"]
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
    for artifact_name in ("selected_plan.json", "review_result.json"):
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

    assert "selected_plan.json" in manifest
    assert "review_result.json" not in manifest
    for artifact_name in artifact_names:
        assert f"planner-1_{artifact_name}" not in manifest
