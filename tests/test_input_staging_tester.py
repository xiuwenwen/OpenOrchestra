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


def test_current_phase_artifacts_are_excluded_from_agent_inputs(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review work")
    previous_phase_id = orchestrator.repository.create_phase(task_id, "TESTING", "tester", 0)
    current_phase_id = orchestrator.repository.create_phase(task_id, "REVIEWING", "reviewer", 0)
    previous_artifact = tmp_path / "bug_report.md"
    current_artifact = tmp_path / "review_result.json"
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

    assert "bug_report.md" not in manifest
    assert "review_result.json" not in manifest


def test_reviewer_receives_latest_complete_tester_result(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review should see tester verdict")
    test_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 0)
    review_phase_id = orchestrator.repository.create_phase(task_id, REVIEWING, "reviewer", 0)
    bug_report = tmp_path / "bug_report.md"
    bug_report.write_text("artifact_result_code: 0\n", encoding="utf-8")
    tester_result = tmp_path / "tester_result.json"
    tester_result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "tests_passed",
                "next_action": "continue",
                "failure_type": "none",
                "environment_dependency_issue": False,
                "summary": "ok",
                "setup_commands_run": [],
                "test_commands_run": [],
                "oracle_results": [],
                "remaining_blockers": [],
            }
        ),
        encoding="utf-8",
    )
    for artifact_type, path in (("bug_report.md", bug_report), ("tester_result.json", tester_result)):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=test_phase_id,
                role="tester",
                agent_id="tester-1",
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "review-input",
        "reviewer",
        REVIEWING,
        exclude_phase_id=review_phase_id,
        round_id=0,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "bug_report.md" in manifest
    assert "tester_result.json" in manifest

def test_tester_receives_no_executor_markdown_artifacts(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("test materialized repository directly")
    execution_phase_id = orchestrator.repository.create_phase(task_id, "EXECUTION", "executor", 0)
    merge_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0)
    for artifact_type, phase_id, agent_id in [
        ("patch.diff", execution_phase_id, "executor-1"),
        ("fix_patch.diff", execution_phase_id, "executor-2"),
        ("merged_patch.diff", merge_phase_id, "executor-1"),
        ("merged_patch_metadata.json", merge_phase_id, "executor-1"),
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
    assert "merged_patch.diff" not in manifest
    assert "patch.diff" not in manifest
    assert "fix_patch.diff" not in manifest

def test_tester_manifest_does_not_include_legacy_test_gate_evidence(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("test with harness gate evidence")
    gate = tmp_path / "test_gate.md"
    gate.write_text(
        "# Harness Test Gate\n\n"
        "status: pass\n"
        "round_id: 2\n\n"
        "## Commands\n\n"
        f"- command: {sys.executable} -m pytest -q\n"
        "  exit_code: 0\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=None,
            role="orchestrator",
            agent_id="test-gate",
            artifact_type="test_gate.md",
            path=gate,
            version=1,
            hash="hash",
        )
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "tester", "TESTING", round_id=2, repo_dir=repo_dir)
    manifest = staged[0].read_text(encoding="utf-8")

    assert "## Harness Test Gate Evidence" not in manifest
    assert "- test_gate_status: pass" not in manifest
    assert f"  - {sys.executable} -m pytest -q" not in manifest


def test_input_staging_reuses_truncated_artifact_cache(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    source = tmp_path / "large.md"
    source.write_text("head\n" + ("body\n" * 1000) + "tail\n", encoding="utf-8")
    artifact = {
        "artifact_id": "artifact-1",
        "version": 1,
        "hash": "hash-1",
    }
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"

    first_size, first_truncated = orchestrator.input_staging_service.copy_artifact_with_budget(
        source,
        first,
        max_file_bytes=256,
        remaining_total_bytes=256,
        artifact=artifact,
    )
    second_size, second_truncated = orchestrator.input_staging_service.copy_artifact_with_budget(
        source,
        second,
        max_file_bytes=256,
        remaining_total_bytes=256,
        artifact=artifact,
    )
    cache_files = list((Path(orchestrator.config["system"]["artifact_root"]).resolve() / "_input_staging_cache").glob("*.artifact"))

    assert first_truncated and second_truncated
    assert first_size == second_size
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
    assert len(cache_files) == 1

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
