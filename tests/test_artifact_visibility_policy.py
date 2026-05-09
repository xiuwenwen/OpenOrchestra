from __future__ import annotations

from pathlib import Path

from harness.artifacts.visibility import ArtifactVisibilityPolicy
from harness.core.state_machine import (
    EXECUTION,
    FIXING,
    PLAN_REVIEW,
    PATCH_MERGE,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEWING,
    TEST_JUDGEMENT,
    TESTING,
)


def _artifact(
    tmp_path: Path,
    phases_by_id: dict[str, dict],
    *,
    role: str | None,
    agent_id: str | None,
    artifact_type: str,
    phase_type: str,
    round_id: int,
    label: str,
    content: str | None = None,
) -> dict:
    phase_id = f"{label}-phase"
    phases_by_id[phase_id] = {"phase_id": phase_id, "phase_type": phase_type, "round_id": round_id}
    path = tmp_path / f"{label}-{artifact_type}"
    path.write_text(content if content is not None else f"round_id: {round_id}\n", encoding="utf-8")
    return {
        "artifact_id": f"{label}-{artifact_type}",
        "phase_id": phase_id,
        "role": role,
        "agent_id": agent_id,
        "artifact_type": artifact_type,
        "path": str(path),
        "version": 1,
    }


def _project_context(tmp_path: Path) -> dict:
    path = tmp_path / "project_context.md"
    path.write_text("context", encoding="utf-8")
    return {
        "artifact_id": "project-context",
        "phase_id": None,
        "role": None,
        "agent_id": None,
        "artifact_type": "project_context.md",
        "path": str(path),
        "version": 1,
    }


def _names(artifacts: list[dict]) -> set[str]:
    return {Path(artifact["path"]).name for artifact in artifacts}


def test_tester_visibility_is_empty_except_project_context(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _project_context(tmp_path),
        _artifact(tmp_path, phases_by_id, role="planner", agent_id="planner-1", artifact_type="plan.md", phase_type=PLANNING_DRAFT, round_id=0, label="plan"),
        _artifact(tmp_path, phases_by_id, role="executor", agent_id="executor-1", artifact_type="merged_patch_metadata.md", phase_type="PATCH_MERGE", round_id=0, label="merge"),
        _artifact(tmp_path, phases_by_id, role="orchestrator", agent_id="patch-validator", artifact_type="patch_validation.md", phase_type="PATCH_GATE", round_id=0, label="gate"),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "tester", TESTING, 0)

    assert _names(visible) == {"project_context.md"}


def test_test_judge_visibility_is_current_test_reports_and_current_gate_only(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(tmp_path, phases_by_id, role="orchestrator", agent_id="test-gate", artifact_type="test_gate.md", phase_type="TEST_GATE", round_id=1, label="current-gate"),
        _artifact(tmp_path, phases_by_id, role="orchestrator", agent_id="objective-gate", artifact_type="objective_gate.md", phase_type="TEST_GATE", round_id=1, label="current-objective"),
        _artifact(tmp_path, phases_by_id, role="orchestrator", agent_id="patch-validator", artifact_type="patch_validation.md", phase_type="TEST_GATE", round_id=1, label="hidden-patch-validation"),
        _artifact(tmp_path, phases_by_id, role="orchestrator", agent_id="test-gate", artifact_type="test_gate.md", phase_type="TEST_GATE", round_id=0, label="old-gate"),
    ]
    for round_id, label in ((0, "old"), (1, "current")):
        artifacts.append(
            _artifact(
                tmp_path,
                phases_by_id,
                role="tester",
                agent_id="tester-1",
                artifact_type="bug_report.md",
                phase_type=TESTING,
                round_id=round_id,
                label=f"{label}-bug_report.md",
            )
        )

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "judge", TEST_JUDGEMENT, 1)

    assert _names(visible) == {
        "current-gate-test_gate.md",
        "current-objective-objective_gate.md",
        "current-bug_report.md-bug_report.md",
    }


def test_fixing_visibility_uses_latest_complete_test_round_before_current(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="bug_report.md", phase_type=TESTING, round_id=0, label="r0-bug"),
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="review_report.md", phase_type=REGRESSION_TESTING, round_id=1, label="r1-non-test-report"),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "executor", FIXING, 2)

    assert _names(visible) == {"r0-bug-bug_report.md"}


def test_planner_peer_review_visibility_is_current_round_other_planners_only(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(tmp_path, phases_by_id, role="planner", agent_id="planner-1", artifact_type="plan.md", phase_type=PLANNING_DRAFT, round_id=1, label="current-self"),
        _artifact(tmp_path, phases_by_id, role="planner", agent_id="planner-2", artifact_type="plan.md", phase_type=PLANNING_DRAFT, round_id=1, label="current-other"),
        _artifact(tmp_path, phases_by_id, role="planner", agent_id="planner-2", artifact_type="risk.md", phase_type=PLANNING_DRAFT, round_id=0, label="old-other"),
        _artifact(tmp_path, phases_by_id, role="planner", agent_id="planner-2", artifact_type="peer_review.md", phase_type=PLANNING_PEER_REVIEW, round_id=1, label="current-peer-review"),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(
        artifacts,
        phases_by_id,
        "planner",
        PLANNING_PEER_REVIEW,
        1,
        current_agent_id="planner-1",
    )

    assert _names(visible) == {"current-other-plan.md"}


def test_executor_execution_visibility_excludes_plan_review_report(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(
            tmp_path,
            phases_by_id,
            role="planner",
            agent_id="planner-1",
            artifact_type="plan.md",
            phase_type=PLANNING_DRAFT,
            round_id=1,
            label="planner",
        ),
        _artifact(
            tmp_path,
            phases_by_id,
            role="reviewer",
            agent_id="reviewer-1",
            artifact_type="selected_plan.md",
            phase_type=PLAN_REVIEW,
            round_id=1,
            label="selected",
        ),
        _artifact(
            tmp_path,
            phases_by_id,
            role="reviewer",
            agent_id="reviewer-1",
            artifact_type="review_report.md",
            phase_type=PLAN_REVIEW,
            round_id=1,
            label="review",
        ),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "executor", EXECUTION, 1)

    assert _names(visible) == {"selected-selected_plan.md"}


def test_executor_review_fixing_visibility_excludes_reviewer_report(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(
            tmp_path,
            phases_by_id,
            role="reviewer",
            agent_id="reviewer-1",
            artifact_type="review_report.md",
            phase_type=REVIEWING,
            round_id=2,
            label="review",
        ),
        _artifact(
            tmp_path,
            phases_by_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="merged_patch_metadata.md",
            phase_type=PATCH_MERGE,
            round_id=1,
            label="metadata",
        ),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(
        artifacts,
        phases_by_id,
        "executor",
        REVIEW_FIXING,
        2,
    )

    assert _names(visible) == {"metadata-merged_patch_metadata.md"}
