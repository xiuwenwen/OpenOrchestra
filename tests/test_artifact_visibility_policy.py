from __future__ import annotations

from pathlib import Path

from harness.artifacts.visibility import ArtifactVisibilityPolicy
from harness.core.state_machine import (
    DELIVERY,
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


def test_tester_visibility_is_empty(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _project_context(tmp_path),
        _artifact(tmp_path, phases_by_id, role="planner", agent_id="planner-1", artifact_type="plan.md", phase_type=PLANNING_DRAFT, round_id=0, label="plan"),
        _artifact(tmp_path, phases_by_id, role="executor", agent_id="executor-1", artifact_type="merged_patch_metadata.json", phase_type="PATCH_MERGE", round_id=0, label="merge"),
        _artifact(tmp_path, phases_by_id, role="orchestrator", agent_id="patch-validator", artifact_type="patch_validation.md", phase_type="PATCH_GATE", round_id=0, label="gate"),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "tester", TESTING, 0)

    assert _names(visible) == set()


def test_tester_visibility_includes_selected_plan_acceptance_contract(tmp_path: Path) -> None:
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
        *[
        _artifact(
            tmp_path,
            phases_by_id,
            role="reviewer",
            agent_id="reviewer-1",
            artifact_type=artifact_type,
            phase_type=PLAN_REVIEW,
            round_id=1,
            label=label,
        )
        for artifact_type, label in (
            ("selected_plan.json", "selected"),
            ("environment_contract.json", "environment"),
            ("validation_contract.json", "validation"),
        )
        ],
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "tester", TESTING, 2)

    assert _names(visible) == {
        "selected-selected_plan.json",
        "environment-environment_contract.json",
        "validation-validation_contract.json",
    }


def test_plan_review_visibility_includes_planner_contract_drafts(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(
            tmp_path,
            phases_by_id,
            role="planner",
            agent_id="planner-1",
            artifact_type=artifact_type,
            phase_type=PLANNING_DRAFT,
            round_id=0,
            label=label,
        )
        for artifact_type, label in (
            ("plan.md", "plan"),
            ("todo_breakdown.json", "todo"),
            ("environment_contract_draft.json", "environment-draft"),
            ("validation_contract_draft.json", "validation-draft"),
        )
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "reviewer", PLAN_REVIEW, 0)

    assert _names(visible) == {
        "plan-plan.md",
        "todo-todo_breakdown.json",
        "environment-draft-environment_contract_draft.json",
        "validation-draft-validation_contract_draft.json",
    }


def test_executor_visibility_includes_final_environment_and_validation_contracts(tmp_path: Path) -> None:
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
        *[
        _artifact(
            tmp_path,
            phases_by_id,
            role="reviewer",
            agent_id="reviewer-1",
            artifact_type=artifact_type,
            phase_type=PLAN_REVIEW,
            round_id=1,
            label=label,
        )
        for artifact_type, label in (
            ("selected_plan.json", "selected"),
            ("environment_contract.json", "environment"),
            ("validation_contract.json", "validation"),
        )
        ],
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "executor", EXECUTION, 1)

    assert _names(visible) == {
        "selected-selected_plan.json",
        "environment-environment_contract.json",
        "validation-validation_contract.json",
    }


def test_project_context_visibility_is_role_scoped(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [_project_context(tmp_path)]

    planner_visible = ArtifactVisibilityPolicy().filter_visible_artifacts(
        artifacts,
        phases_by_id,
        "planner",
        PLANNING_DRAFT,
        0,
    )
    tester_visible = ArtifactVisibilityPolicy().filter_visible_artifacts(
        artifacts,
        phases_by_id,
        "tester",
        TESTING,
        0,
    )

    assert _names(planner_visible) == {"project_context.md"}
    assert _names(tester_visible) == set()


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
        artifacts.append(
            _artifact(
                tmp_path,
                phases_by_id,
                role="tester",
                agent_id="tester-1",
                artifact_type="tester_result.json",
                phase_type=TESTING,
                round_id=round_id,
                label=f"{label}-tester_result.json",
            )
        )

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "judge", TEST_JUDGEMENT, 1)

    assert _names(visible) == {
        "current-objective-objective_gate.md",
        "current-bug_report.md-bug_report.md",
        "current-tester_result.json-tester_result.json",
    }


def test_fixing_visibility_uses_latest_complete_test_round_before_current(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="bug_report.md", phase_type=TESTING, round_id=0, label="r0-bug"),
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="tester_result.json", phase_type=TESTING, round_id=0, label="r0-result"),
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="notes.md", phase_type=REGRESSION_TESTING, round_id=1, label="r1-non-test-report"),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "executor", FIXING, 2)

    assert _names(visible) == {"r0-bug-bug_report.md", "r0-result-tester_result.json"}


def test_tester_environment_repair_visibility_keeps_current_round_tester_artifacts(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="bug_report.md", phase_type=TESTING, round_id=0, label="retry-bug"),
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="tester_result.json", phase_type=TESTING, round_id=0, label="retry-result"),
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="bug_report.md", phase_type=TESTING, round_id=1, label="other-round"),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "tester", TESTING, 0)

    assert _names(visible) == {"retry-bug-bug_report.md", "retry-result-tester_result.json"}


def test_planner_peer_review_visibility_is_current_round_other_planners_only(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(tmp_path, phases_by_id, role="planner", agent_id="planner-1", artifact_type="plan.md", phase_type=PLANNING_DRAFT, round_id=1, label="current-self"),
        _artifact(tmp_path, phases_by_id, role="planner", agent_id="planner-2", artifact_type="plan.md", phase_type=PLANNING_DRAFT, round_id=1, label="current-other"),
        _artifact(tmp_path, phases_by_id, role="planner", agent_id="planner-2", artifact_type="risk.md", phase_type=PLANNING_DRAFT, round_id=0, label="old-other"),
        _artifact(tmp_path, phases_by_id, role="planner", agent_id="planner-2", artifact_type="peer_review_result.json", phase_type=PLANNING_PEER_REVIEW, round_id=1, label="current-peer-review"),
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


def test_executor_execution_visibility_excludes_plan_review_result(tmp_path: Path) -> None:
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
            artifact_type="selected_plan.json",
            phase_type=PLAN_REVIEW,
            round_id=1,
            label="selected",
        ),
        _artifact(
            tmp_path,
            phases_by_id,
            role="reviewer",
            agent_id="reviewer-1",
            artifact_type="review_result.json",
            phase_type=PLAN_REVIEW,
            round_id=1,
            label="review",
        ),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "executor", EXECUTION, 1)

    assert _names(visible) == {"selected-selected_plan.json"}


def test_executor_review_fixing_visibility_excludes_reviewer_result(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(
            tmp_path,
            phases_by_id,
            role="reviewer",
            agent_id="reviewer-1",
            artifact_type="review_result.json",
            phase_type=REVIEWING,
            round_id=2,
            label="review",
        ),
        _artifact(
            tmp_path,
            phases_by_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="merged_patch_metadata.json",
            phase_type=PATCH_MERGE,
            round_id=1,
            label="metadata",
        ),
        _artifact(
            tmp_path,
            phases_by_id,
            role="orchestrator",
            agent_id="objective-gate",
            artifact_type="objective_gate.md",
            phase_type=PATCH_MERGE,
            round_id=1,
            label="objective",
        ),
        _artifact(
            tmp_path,
            phases_by_id,
            role="orchestrator",
            agent_id="patch-validator",
            artifact_type="patch_gate_result.json",
            phase_type=PATCH_MERGE,
            round_id=1,
            label="patch-gate",
            content='{"round_id": 1, "status": "fail", "failure_type": "patch_apply"}\n',
        ),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(
        artifacts,
        phases_by_id,
        "executor",
        REVIEW_FIXING,
        2,
    )

    assert _names(visible) == {
        "metadata-merged_patch_metadata.json",
        "objective-objective_gate.md",
        "patch-gate-patch_gate_result.json",
    }


def test_reviewer_visibility_includes_structured_tester_report_but_excludes_gate_noise(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(tmp_path, phases_by_id, role="reviewer", agent_id="reviewer-1", artifact_type="selected_plan.json", phase_type=PLAN_REVIEW, round_id=1, label="selected"),
        _artifact(tmp_path, phases_by_id, role="executor", agent_id="executor-1", artifact_type="merged_patch.diff", phase_type=PATCH_MERGE, round_id=3, label="merged"),
        _artifact(tmp_path, phases_by_id, role="executor", agent_id="executor-1", artifact_type="self_check.md", phase_type=PATCH_MERGE, round_id=3, label="self"),
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="bug_report.md", phase_type=TESTING, round_id=3, label="bug"),
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="tester_result.json", phase_type=TESTING, round_id=3, label="result"),
        _artifact(tmp_path, phases_by_id, role="judge", agent_id="judge-1", artifact_type="decision.json", phase_type=TEST_JUDGEMENT, round_id=3, label="judge"),
        _artifact(tmp_path, phases_by_id, role="orchestrator", agent_id="orchestrator", artifact_type="test_gate.md", phase_type="TEST_GATE", round_id=3, label="gate"),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "reviewer", REVIEWING, 0)

    assert _names(visible) == {
        "selected-selected_plan.json",
        "merged-merged_patch.diff",
        "self-self_check.md",
        "bug-bug_report.md",
        "result-tester_result.json",
    }


def test_latest_visibility_uses_round_before_input_order(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    old_metadata = _artifact(
        tmp_path,
        phases_by_id,
        role="executor",
        agent_id="executor-1",
        artifact_type="merged_patch_metadata.json",
        phase_type=PATCH_MERGE,
        round_id=1,
        label="old-metadata",
    )
    old_metadata["version"] = 99
    latest_metadata = _artifact(
        tmp_path,
        phases_by_id,
        role="executor",
        agent_id="executor-1",
        artifact_type="merged_patch_metadata.json",
        phase_type=PATCH_MERGE,
        round_id=3,
        label="latest-metadata",
    )
    latest_metadata["version"] = 1
    artifacts = [latest_metadata, old_metadata]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "reviewer", REVIEWING, 0)

    assert _names(visible) == {"latest-metadata-merged_patch_metadata.json"}


def test_latest_visibility_uses_version_when_rounds_match(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    old_version = _artifact(
        tmp_path,
        phases_by_id,
        role="executor",
        agent_id="executor-1",
        artifact_type="merged_patch_metadata.json",
        phase_type=PATCH_MERGE,
        round_id=3,
        label="old-version",
    )
    old_version["version"] = 1
    latest_version = _artifact(
        tmp_path,
        phases_by_id,
        role="executor",
        agent_id="executor-1",
        artifact_type="merged_patch_metadata.json",
        phase_type=PATCH_MERGE,
        round_id=3,
        label="latest-version",
    )
    latest_version["version"] = 3
    artifacts = [latest_version, old_version]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "reviewer", REVIEWING, 0)

    assert _names(visible) == {"latest-version-merged_patch_metadata.json"}


def test_latest_visibility_uses_declared_round_when_phase_row_is_missing(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    old_metadata = _artifact(
        tmp_path,
        phases_by_id,
        role="executor",
        agent_id="executor-1",
        artifact_type="merged_patch_metadata.json",
        phase_type=PATCH_MERGE,
        round_id=1,
        label="declared-old",
        content="round_id: 1\n",
    )
    old_metadata["phase_id"] = "missing-old-phase"
    latest_metadata = _artifact(
        tmp_path,
        phases_by_id,
        role="executor",
        agent_id="executor-1",
        artifact_type="merged_patch_metadata.json",
        phase_type=PATCH_MERGE,
        round_id=5,
        label="declared-latest",
        content="round_id: 5\n",
    )
    latest_metadata["phase_id"] = "missing-latest-phase"
    artifacts = [latest_metadata, old_metadata]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "reviewer", REVIEWING, 0)

    assert _names(visible) == {"declared-latest-merged_patch_metadata.json"}


def test_communicator_visibility_uses_only_plan_and_final_executor_artifacts(tmp_path: Path) -> None:
    phases_by_id: dict[str, dict] = {}
    artifacts = [
        _artifact(tmp_path, phases_by_id, role="reviewer", agent_id="reviewer-1", artifact_type="selected_plan.json", phase_type=PLAN_REVIEW, round_id=1, label="selected"),
        _artifact(tmp_path, phases_by_id, role="executor", agent_id="executor-1", artifact_type="merged_patch.diff", phase_type=PATCH_MERGE, round_id=3, label="merged"),
        _artifact(tmp_path, phases_by_id, role="executor", agent_id="executor-1", artifact_type="merged_patch_metadata.json", phase_type=PATCH_MERGE, round_id=3, label="metadata"),
        _artifact(tmp_path, phases_by_id, role="executor", agent_id="executor-1", artifact_type="changed_files.md", phase_type=PATCH_MERGE, round_id=3, label="changed"),
        _artifact(tmp_path, phases_by_id, role="executor", agent_id="executor-1", artifact_type="self_check.md", phase_type=PATCH_MERGE, round_id=3, label="self"),
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="bug_report.md", phase_type=TESTING, round_id=3, label="bug"),
        _artifact(tmp_path, phases_by_id, role="tester", agent_id="tester-1", artifact_type="tester_result.json", phase_type=TESTING, round_id=3, label="result"),
        _artifact(tmp_path, phases_by_id, role="reviewer", agent_id="reviewer-1", artifact_type="review_result.json", phase_type=REVIEWING, round_id=1, label="review"),
        _artifact(tmp_path, phases_by_id, role="judge", agent_id="judge-1", artifact_type="decision.json", phase_type=TEST_JUDGEMENT, round_id=3, label="judge"),
        _artifact(tmp_path, phases_by_id, role="orchestrator", agent_id="orchestrator", artifact_type="test_gate.md", phase_type="TEST_GATE", round_id=3, label="gate"),
    ]

    visible = ArtifactVisibilityPolicy().filter_visible_artifacts(artifacts, phases_by_id, "communicator", DELIVERY, 0)

    assert _names(visible) == {
        "selected-selected_plan.json",
        "metadata-merged_patch_metadata.json",
        "changed-changed_files.md",
        "self-self_check.md",
        "bug-bug_report.md",
        "result-tester_result.json",
    }
