from __future__ import annotations

import uuid
from pathlib import Path

from harness.agents.result import ArtifactRef
from harness.core.orchestrator import Orchestrator
from harness.core.state_machine import DELIVERY, PATCH_MERGE, PLAN_REVIEW, REVIEWING, TESTING, TEST_JUDGEMENT


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
        },
        "agent_backend": {"default": "mock"},
        "roles": {
            "planner": {"count": 2},
            "executor": {"count": 2},
            "tester": {"count": 1},
            "reviewer": {"count": 1},
            "judge": {"count": 1},
            "communicator": {"count": 1},
        },
        "limits": {"max_agent_retry": 1},
        "timeouts": {
            "planner": 5,
            "executor": 5,
            "tester": 5,
            "reviewer": 5,
            "judge": 5,
            "communicator": 5,
        },
        "artifact_input": {"max_files": 50, "max_file_bytes": 262_144, "max_total_bytes": 1_048_576},
    }


def test_role_phase_budget_defaults_are_used_before_global_caps(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))

    assert orchestrator._artifact_input_limits("tester", TESTING)["max_files"] == 8
    assert orchestrator._artifact_input_limits("judge", TEST_JUDGEMENT)["max_files"] == 8
    assert orchestrator._artifact_input_limits("reviewer", REVIEWING)["max_files"] == 12
    assert orchestrator._artifact_input_limits("communicator", DELIVERY)["max_files"] == 8


def test_global_artifact_input_limit_still_caps_role_phase_budget(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["artifact_input"]["max_files"] = 1
    orchestrator = Orchestrator(config)

    assert orchestrator._artifact_input_limits("reviewer", REVIEWING)["max_files"] == 1


def test_role_phase_artifact_input_override_limits_staged_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["artifact_input"] = {
        "max_files": 50,
        "max_file_bytes": 262_144,
        "max_total_bytes": 1_048_576,
        "role_phase": {"reviewer": {REVIEWING: {"max_files": 2}}},
    }
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("review budget")
    plan_phase_id = orchestrator.repository.create_phase(task_id, PLAN_REVIEW, "reviewer", 0)
    merge_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0)
    current_phase_id = orchestrator.repository.create_phase(task_id, REVIEWING, "reviewer", 0)

    for artifact_type, phase_id, role, filename in (
        ("selected_plan.json", plan_phase_id, "reviewer", "selected-plan.json"),
        ("changed_files.md", merge_phase_id, "executor", "changed-files.md"),
        ("merged_patch_metadata.json", merge_phase_id, "executor", "metadata.json"),
        ("self_check.md", merge_phase_id, "executor", "self-check.md"),
    ):
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
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

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "input",
        "reviewer",
        REVIEWING,
        exclude_phase_id=current_phase_id,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert len(staged) == 3
    assert manifest.count("- skipped: true") == 2
    assert "max_files exceeded" in manifest


def test_role_phase_large_artifact_mode_controls_large_diff_staging(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    source = tmp_path / "merged_patch.diff"
    source.write_text("x" * 64_001, encoding="utf-8")
    artifact = {"artifact_type": "merged_patch.diff"}

    tester_limits = orchestrator._artifact_input_limits("tester", TESTING)
    reviewer_limits = orchestrator._artifact_input_limits("reviewer", REVIEWING)

    assert tester_limits["large_artifact_mode"] == "path_only"
    assert reviewer_limits["large_artifact_mode"] == "truncated"
    assert (
        orchestrator._artifact_staging_mode(
            "tester",
            TESTING,
            artifact,
            source,
            large_artifact_mode=str(tester_limits["large_artifact_mode"]),
        )
        == "path_only"
    )
    assert (
        orchestrator._artifact_staging_mode(
            "reviewer",
            REVIEWING,
            artifact,
            source,
            large_artifact_mode=str(reviewer_limits["large_artifact_mode"]),
        )
        == "truncated"
    )
