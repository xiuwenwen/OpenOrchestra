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
    FIXING,
    PATCH_MERGE,
    PLAN_REVIEW,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEWING,
    RUNNING,
    TESTING,
)
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, NEW_PROJECT
from harness.patch.gate import materialized_repo_markdown, run_patch_gate


from orchestrator_mock_support import _config


def test_orchestrator_bugfix_flow_uses_persisted_workflow_and_runs_review(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("fix a failing command", workflow_type=BUGFIX)

    final_delivery = orchestrator.run_task(task_id)

    phases = [phase["phase_type"] for phase in orchestrator.repository.list_phases(task_id)]
    assert "PLANNING_DRAFT" in phases
    assert "PLAN_REVIEW" in phases
    assert "FIXING" in phases
    assert "TESTING" in phases
    assert "REVIEWING" in phases
    assert final_delivery.exists()

def test_reviewer_stages_only_plan_executor_and_test_evidence(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review final implementation")
    plan_review_phase_id = orchestrator.repository.create_phase(task_id, PLAN_REVIEW, "reviewer", 0)
    merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 3)
    testing_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 3)
    current_phase_id = orchestrator.repository.create_phase(task_id, REVIEWING, "reviewer", 0)

    artifact_rows = [
        ("selected_plan.json", plan_review_phase_id, "reviewer", "selected-plan.json", "reviewer-1"),
        ("merged_patch.diff", merge_phase_id, "executor", "merged.patch", "executor-1"),
        ("merged_patch_metadata.json", merge_phase_id, "executor", "merged-metadata.json", "executor-1"),
        ("changed_files.md", merge_phase_id, "executor", "changed-files.md", "executor-1"),
        ("self_check.md", merge_phase_id, "executor", "self-check.md", "executor-1"),
        ("fix_schedule.md", merge_phase_id, "executor", "fix-schedule.md", "executor-1"),
        ("fix_notes.md", merge_phase_id, "executor", "fix-notes.md", "executor-1"),
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

    for artifact_type, filename in (
        ("test_gate.md", "test-gate.md"),
        ("objective_gate.md", "objective-gate.md"),
        ("patch_validation.md", "patch-validation.md"),
        ("materialized_repo.md", "materialized-repo.md"),
    ):
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=None,
                role="orchestrator",
                agent_id="orchestrator",
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "reviewer-input",
        "reviewer",
        REVIEWING,
        exclude_phase_id=current_phase_id,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "selected-plan.json" in manifest
    assert "merged.patch" in manifest
    assert "merged-metadata.json" in manifest
    assert "changed-files.md" in manifest
    assert "self-check.md" in manifest

    assert "bug-report.md" not in manifest
    assert "fix-schedule.md" not in manifest
    assert "fix-notes.md" not in manifest
    assert "test-gate.md" not in manifest
    assert "objective-gate.md" not in manifest
    assert "patch-validation.md" not in manifest
    assert "materialized-repo.md" not in manifest

def test_review_loop_fails_immediately_on_blocked_environment_json(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("deliver runtime-sensitive project")
    real_run_role_phase = orchestrator.run_role_phase

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        results = real_run_role_phase(role, phase, round_id, required_outputs, user_prompt, **kwargs)
        if phase == REVIEWING:
            review_result = next(ref.path for result in results for ref in result.artifacts if ref.artifact_type == "review_result.json")
            review_result.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "review_decision_code": 2,
                        "summary": "Runtime blocked by incompatible platform dependency.",
                        "findings": [],
                        "required_changes": [],
                        "environment_check": {
                            "attempted": True,
                            "status": "blocked",
                            "commands_run": ["pip install -r requirements.txt", "python app.py"],
                            "fixable": False,
                            "blocking_reason": "requires Linux-only system package not available on this machine",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        return results

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)

    with pytest.raises(orchestrator_module.TaskFailedError, match="requires Linux-only system package"):
        orchestrator._run_review_loop(task_id, "deliver runtime-sensitive project")


def test_review_environment_changes_required_routes_to_tester_not_executor(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review with fixable environment issue")
    calls: list[tuple[str, str]] = []

    def result_for(role: str, phase: str, artifact_type: str, path: Path) -> list[AgentRunResult]:
        ref = ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=f"{phase}-phase",
            role=role,
            agent_id=f"{role}-1",
            artifact_type=artifact_type,
            path=path,
            version=1,
            hash="hash",
        )
        return [AgentRunResult(task_id, f"{phase}-phase", role, f"{role}-1", "COMPLETED", artifacts=[ref])]

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        calls.append((role, phase))
        if phase == REVIEWING:
            review_result = tmp_path / "review_result.json"
            review_result.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "review_decision_code": 0,
                        "summary": "source is approved; local runtime needs environment follow-up",
                        "findings": [],
                        "required_changes": [],
                        "environment_check": {
                            "attempted": True,
                            "status": "changes_required",
                            "commands_run": ["python -m pytest"],
                            "fixable": True,
                            "blocking_reason": "install test dependencies in tester environment",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return result_for("reviewer", phase, "review_result.json", review_result)
        if phase == REGRESSION_TESTING:
            tester_result = tmp_path / "tester_result.json"
            tester_result.write_text(
                json.dumps(
                    {
                        "status": "tests_passed",
                        "next_action": "continue",
                        "failure_type": "none",
                        "summary": "environment repaired and regression checks passed",
                        "environment_dependency_issue": False,
                        "oracle_results": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return result_for("tester", phase, "tester_result.json", tester_result)
        if phase == REVIEW_FIXING:
            raise AssertionError("environment follow-up must not be routed to executor source fixing")
        raise AssertionError(f"unexpected phase: {role} {phase}")

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)

    orchestrator._run_review_loop(task_id, "review with fixable environment issue")

    assert ("tester", REGRESSION_TESTING) in calls
    assert ("executor", REVIEW_FIXING) not in calls


def test_review_environment_changes_required_reuses_passing_tester_result(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review with prior tester pass")
    testing_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 0)
    tester_result = tmp_path / "prior_tester_result.json"
    tester_result.write_text(
        json.dumps(
            {
                "status": "tests_passed",
                "next_action": "continue",
                "failure_type": "none",
                "summary": "prior tester pass",
                "environment_dependency_issue": False,
                "oracle_results": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=testing_phase_id,
            role="tester",
            agent_id="tester-1",
            artifact_type="tester_result.json",
            path=tester_result,
            version=1,
            hash="hash",
        )
    )
    calls: list[tuple[str, str]] = []

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        calls.append((role, phase))
        if phase == REVIEWING:
            review_result = tmp_path / "review_result.json"
            review_result.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "review_decision_code": 0,
                        "summary": "source approved; reviewer local env is incomplete",
                        "findings": [],
                        "required_changes": [],
                        "environment_check": {
                            "attempted": True,
                            "status": "changes_required",
                            "commands_run": ["python -m pytest"],
                            "fixable": True,
                            "blocking_reason": "reviewer local env cannot build old dependency",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            ref = ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id="review-phase",
                role="reviewer",
                agent_id="reviewer-1",
                artifact_type="review_result.json",
                path=review_result,
                version=1,
                hash="hash",
            )
            return [AgentRunResult(task_id, "review-phase", "reviewer", "reviewer-1", "COMPLETED", artifacts=[ref])]
        raise AssertionError(f"unexpected route to {role} {phase}")

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)

    orchestrator._run_review_loop(task_id, "review with prior tester pass")

    assert calls == [("reviewer", REVIEWING)]


def test_unrouteable_review_result_does_not_fall_through_to_executor(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review invalid route")
    calls: list[tuple[str, str]] = []

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        calls.append((role, phase))
        if phase == REVIEWING:
            review_result = tmp_path / "review_result.json"
            review_result.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "review_decision_code": 0,
                        "summary": "invalid env status",
                        "findings": [],
                        "required_changes": [],
                        "environment_check": {
                            "attempted": True,
                            "status": "unknown",
                            "commands_run": [],
                            "fixable": True,
                            "blocking_reason": "",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            ref = ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id="review-phase",
                role="reviewer",
                agent_id="reviewer-1",
                artifact_type="review_result.json",
                path=review_result,
                version=1,
                hash="hash",
            )
            return [AgentRunResult(task_id, "review-phase", "reviewer", "reviewer-1", "COMPLETED", artifacts=[ref])]
        if phase == REVIEW_FIXING:
            raise AssertionError("unrouteable review artifacts must not be treated as source fixes")
        raise AssertionError(f"unexpected route to {role} {phase}")

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)

    with pytest.raises(orchestrator_module.TaskFailedError, match="unrouteable review_result"):
        orchestrator._run_review_loop(task_id, "review invalid route")

    assert calls == [("reviewer", REVIEWING)]


def test_review_approval_requires_review_result_environment_check(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review strict environment verdict")
    phase_id = "review-phase"
    report_path = tmp_path / "review_result.json"
    report_ref = ArtifactRef(
        artifact_id=str(uuid.uuid4()),
        task_id=task_id,
        phase_id=phase_id,
        role="reviewer",
        agent_id="reviewer-1",
        artifact_type="review_result.json",
        path=report_path,
        version=1,
        hash="hash",
    )
    result = AgentRunResult(task_id, phase_id, "reviewer", "reviewer-1", "COMPLETED", artifacts=[report_ref])

    report_path.write_text(json.dumps({"review_decision_code": 0}) + "\n", encoding="utf-8")
    assert not orchestrator.workflow_engine.review_approved([result])

    report_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "review_decision_code": 0,
                "summary": "ready",
                "findings": [],
                "required_changes": [],
                "environment_check": {
                    "attempted": True,
                    "status": "ready",
                    "commands_run": ["pytest"],
                    "fixable": True,
                    "blocking_reason": "",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert orchestrator.workflow_engine.review_approved([result])


def test_review_approval_reads_review_result_json(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review json verdict")
    phase_id = "review-phase"
    report_path = tmp_path / "review_result.json"
    report_ref = ArtifactRef(
        artifact_id=str(uuid.uuid4()),
        task_id=task_id,
        phase_id=phase_id,
        role="reviewer",
        agent_id="reviewer-1",
        artifact_type="review_result.json",
        path=report_path,
        version=1,
        hash="hash",
    )
    result = AgentRunResult(task_id, phase_id, "reviewer", "reviewer-1", "COMPLETED", artifacts=[report_ref])
    payload = {
        "schema_version": 1,
        "review_decision_code": 0,
        "summary": "approved",
        "findings": [],
        "required_changes": [],
        "environment_check": {
            "attempted": True,
            "status": "ready",
            "commands_run": ["pytest"],
            "fixable": True,
            "blocking_reason": "",
        },
    }
    report_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert orchestrator.workflow_engine.review_approved([result])


@pytest.mark.parametrize(
    ("decision_code", "environment_check", "expected"),
    [
        (0, {"status": "ready", "attempted": True, "commands_run": ["pytest"]}, True),
        (0, {"status": "not_applicable", "attempted": False, "commands_run": []}, True),
        (0, {"status": "changes_required", "attempted": True, "commands_run": ["pytest"]}, False),
        (0, {"status": "blocked", "attempted": True, "commands_run": ["python app.py"]}, False),
        (0, None, False),
        (1, {"status": "ready", "attempted": True, "commands_run": ["pytest"]}, False),
        (2, {"status": "ready", "attempted": True, "commands_run": ["pytest"]}, False),
    ],
)

def test_review_approval_requires_positive_code_and_safe_environment(
    tmp_path: Path,
    decision_code: int,
    environment_check: dict | None,
    expected: bool,
) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review verdict matrix")
    report_path = tmp_path / "review_result.json"
    report_ref = ArtifactRef(
        artifact_id=str(uuid.uuid4()),
        task_id=task_id,
        phase_id="review-phase",
        role="reviewer",
        agent_id="reviewer-1",
        artifact_type="review_result.json",
        path=report_path,
        version=1,
        hash="hash",
    )
    result = AgentRunResult(task_id, "review-phase", "reviewer", "reviewer-1", "COMPLETED", artifacts=[report_ref])
    payload = {
        "schema_version": 1,
        "review_decision_code": decision_code,
        "summary": "matrix",
        "findings": [],
        "required_changes": [],
        "environment_check": environment_check,
    }
    if isinstance(environment_check, dict):
        environment_check.setdefault("fixable", True)
        environment_check.setdefault("blocking_reason", "")
    report_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert orchestrator.workflow_engine.review_approved([result]) is expected
