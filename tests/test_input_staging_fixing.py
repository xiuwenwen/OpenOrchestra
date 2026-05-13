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


def test_input_staging_ignores_previous_prompt_turn_phase_artifacts(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("merge followup patch only")
    old_fix_phase_id = orchestrator.repository.create_phase(task_id, FIXING, "executor", 0)
    old_patch = tmp_path / "old-fix.patch"; old_patch.write_text("old", encoding="utf-8")
    orchestrator.repository.create_artifact(ArtifactRef(str(uuid.uuid4()), task_id, old_fix_phase_id, "executor", "executor-1", "fix_patch.diff", old_patch, 1, "hash"))
    orchestrator.repository.append_task_prompt_turn(task_id, "second turn")
    current_fix_phase_id = orchestrator.repository.create_phase(task_id, FIXING, "executor", 0)
    current_merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0)
    current_patch = tmp_path / "current-fix.patch"; current_patch.write_text("current", encoding="utf-8")
    orchestrator.repository.create_artifact(ArtifactRef(str(uuid.uuid4()), task_id, current_fix_phase_id, "executor", "executor-1", "fix_patch.diff", current_patch, 2, "hash"))

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input-followup", "executor", "PATCH_MERGE", exclude_phase_id=current_merge_phase_id, round_id=0)
    manifest = staged[0].read_text(encoding="utf-8")

    assert "current-fix.patch" in manifest
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
    assert "round-1-objective_gate.md" in manifest

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
    assert "round-1-test_gate.md" not in manifest
    assert "round-1-patch_validation.md" not in manifest
    assert "round-1-materialized_repo.md" not in manifest
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

def test_staged_input_artifacts_respect_size_budget(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["artifact_input"] = {"max_files": 1, "max_file_bytes": 40, "max_total_bytes": 60}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("review large artifacts")
    executor_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0)
    small_self_check = tmp_path / "self_check.md"
    small_self_check.write_text("self", encoding="utf-8")
    large_patch = tmp_path / "merged_patch.diff"
    large_patch.write_text("diff line\n" * 200, encoding="utf-8")
    for artifact_type, phase_id, role, path in (
        ("self_check.md", executor_phase_id, "executor", small_self_check),
        ("merged_patch.diff", executor_phase_id, "executor", large_patch),
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
    assert staged[1].stat().st_size < large_patch.stat().st_size

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
