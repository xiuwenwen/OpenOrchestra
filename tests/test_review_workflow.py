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


def test_orchestrator_bugfix_flow_uses_persisted_workflow_and_runs_review(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("fix a failing command", workflow_type=BUGFIX)

    final_delivery = orchestrator.run_task(task_id)

    phases = [phase["phase_type"] for phase in orchestrator.repository.list_phases(task_id)]
    assert "PLANNING_DRAFT" not in phases
    assert "FIXING" in phases
    assert "TESTING" in phases
    assert "REVIEWING" in phases
    assert "REVIEW_JUDGEMENT" not in phases
    assert FINAL_JUDGEMENT not in phases
    assert final_delivery.exists()

def test_reviewer_stages_only_plan_executor_and_test_evidence(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review final implementation")
    plan_review_phase_id = orchestrator.repository.create_phase(task_id, PLAN_REVIEW, "reviewer", 0)
    merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 3)
    testing_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 3)
    judge_phase_id = orchestrator.repository.create_phase(task_id, TEST_JUDGEMENT, "judge", 3)
    current_phase_id = orchestrator.repository.create_phase(task_id, REVIEWING, "reviewer", 0)

    artifact_rows = [
        ("selected_plan.md", plan_review_phase_id, "reviewer", "selected-plan.md", "reviewer-1"),
        ("merged_patch.diff", merge_phase_id, "executor", "merged.patch", "executor-1"),
        ("merged_patch_metadata.md", merge_phase_id, "executor", "merged-metadata.md", "executor-1"),
        ("changed_files.md", merge_phase_id, "executor", "changed-files.md", "executor-1"),
        ("self_check.md", merge_phase_id, "executor", "self-check.md", "executor-1"),
        ("fix_schedule.md", merge_phase_id, "executor", "fix-schedule.md", "executor-1"),
        ("fix_notes.md", merge_phase_id, "executor", "fix-notes.md", "executor-1"),
        ("merge_report.md", merge_phase_id, "executor", "merge-report.md", "executor-1"),
        ("decision.json", judge_phase_id, "judge", "test-decision.json", "judge-1"),
        ("decision_summary.md", judge_phase_id, "judge", "test-decision-summary.md", "judge-1"),
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

    assert "selected-plan.md" in manifest
    assert "merged.patch" in manifest
    assert "merged-metadata.md" in manifest
    assert "changed-files.md" in manifest
    assert "self-check.md" in manifest

    assert "bug-report.md" not in manifest
    assert "fix-schedule.md" not in manifest
    assert "fix-notes.md" not in manifest
    assert "merge-report.md" not in manifest
    assert "test-decision.json" not in manifest
    assert "test-decision-summary.md" not in manifest
    assert "test-gate.md" not in manifest
    assert "objective-gate.md" not in manifest
    assert "patch-validation.md" not in manifest
    assert "materialized-repo.md" not in manifest

def test_review_judgement_legacy_visibility_is_lean_review_and_metadata_only(tmp_path: Path) -> None:
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
    assert "current-review-report.md" in manifest

    assert "old-merged-metadata.md" not in manifest
    assert "old-bug-report.md" not in manifest
    assert "latest-bug-report.md" not in manifest
    assert "review-judge-round-1-test_gate.md" not in manifest
    assert "review-judge-round-1-objective_gate.md" not in manifest
    assert "review-judge-round-1-patch_validation.md" not in manifest
    assert "review-judge-round-1-materialized_repo.md" not in manifest
    assert "review-judge-round-2-test_gate.md" not in manifest
    assert "review-judge-round-2-objective_gate.md" not in manifest
    assert "review-judge-round-2-patch_validation.md" not in manifest
    assert "review-judge-round-2-materialized_repo.md" not in manifest

def test_review_loop_fails_immediately_on_blocked_environment_json(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("deliver runtime-sensitive project")
    real_run_role_phase = orchestrator.run_role_phase

    def fake_run_role_phase(role: str, phase: str, round_id: int, required_outputs: list[str], user_prompt: str, **kwargs):
        results = real_run_role_phase(role, phase, round_id, required_outputs, user_prompt, **kwargs)
        if phase == REVIEWING:
            review_report = next(ref.path for result in results for ref in result.artifacts if ref.artifact_type == "review_report.md")
            review_report.write_text(
                "artifact_result_code: 0\n\n"
                "# Review Report\n\n"
                "review_decision_code: -1\n"
                "Runtime blocked by incompatible platform dependency.\n\n"
                "## Review Verdict JSON\n\n"
                "```json\n"
                "{\n"
                '  "review_status": "blocked",\n'
                '  "environment_check": {\n'
                '    "attempted": true,\n'
                '    "status": "blocked",\n'
                '    "commands_run": ["pip install -r requirements.txt", "python app.py"],\n'
                '    "fixable": false,\n'
                '    "blocking_reason": "requires Linux-only system package not available on this machine"\n'
                "  }\n"
                "}\n"
                "```\n",
                encoding="utf-8",
            )
        return results

    monkeypatch.setattr(orchestrator, "run_role_phase", fake_run_role_phase)
    monkeypatch.setattr(
        orchestrator,
        "_run_judge_phase",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("judge should not run after blocked reviewer environment verdict")),
    )

    with pytest.raises(orchestrator_module.TaskFailedError, match="requires Linux-only system package"):
        orchestrator._run_review_loop(task_id, "deliver runtime-sensitive project")

def test_review_approval_requires_verdict_json_environment_check(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review strict environment verdict")
    phase_id = "review-phase"
    report_path = tmp_path / "review_report.md"
    report_ref = ArtifactRef(
        artifact_id=str(uuid.uuid4()),
        task_id=task_id,
        phase_id=phase_id,
        role="reviewer",
        agent_id="reviewer-1",
        artifact_type="review_report.md",
        path=report_path,
        version=1,
        hash="hash",
    )
    result = AgentRunResult(task_id, phase_id, "reviewer", "reviewer-1", "COMPLETED", artifacts=[report_ref])

    report_path.write_text("artifact_result_code: 0\n\nreview_decision_code: 0\n", encoding="utf-8")
    assert not orchestrator.workflow_engine.review_approved([result])

    report_path.write_text(
        "artifact_result_code: 0\n\n"
        "review_decision_code: 0\n\n"
        "## Review Verdict JSON\n\n"
        "```json\n"
        '{"review_status":"approved","environment_check":{"attempted":true,"status":"ready","commands_run":["pytest"],"fixable":true,"blocking_reason":""}}\n'
        "```\n",
        encoding="utf-8",
    )
    assert orchestrator.workflow_engine.review_approved([result])


@pytest.mark.parametrize(
    ("decision_code", "review_status", "environment_check", "expected"),
    [
        (0, "approved", {"status": "ready", "attempted": True, "commands_run": ["pytest"]}, True),
        (0, "approved", {"status": "not_applicable", "attempted": False, "commands_run": []}, True),
        (0, "approved", {"status": "blocked", "attempted": True, "commands_run": ["python app.py"]}, False),
        (0, "changes_requested", {"status": "ready", "attempted": True, "commands_run": ["pytest"]}, False),
        (0, "approved", None, False),
        (1, "approved", {"status": "ready", "attempted": True, "commands_run": ["pytest"]}, False),
    ],
)

def test_review_approval_requires_positive_code_approved_status_and_safe_environment(
    tmp_path: Path,
    decision_code: int,
    review_status: str,
    environment_check: dict | None,
    expected: bool,
) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review verdict matrix")
    report_path = tmp_path / "review_report.md"
    report_ref = ArtifactRef(
        artifact_id=str(uuid.uuid4()),
        task_id=task_id,
        phase_id="review-phase",
        role="reviewer",
        agent_id="reviewer-1",
        artifact_type="review_report.md",
        path=report_path,
        version=1,
        hash="hash",
    )
    result = AgentRunResult(task_id, "review-phase", "reviewer", "reviewer-1", "COMPLETED", artifacts=[report_ref])
    payload = {"review_status": review_status, "environment_check": environment_check}
    report_path.write_text(
        "artifact_result_code: 0\n\n"
        f"review_decision_code: {decision_code}\n\n"
        "## Review Verdict JSON\n\n"
        "```json\n"
        f"{json.dumps(payload)}\n"
        "```\n",
        encoding="utf-8",
    )

    assert orchestrator.workflow_engine.review_approved([result]) is expected
