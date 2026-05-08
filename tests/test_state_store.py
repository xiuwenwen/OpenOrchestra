from __future__ import annotations

import uuid
import sqlite3
from pathlib import Path

import pytest

from harness.agents.result import ArtifactRef
from harness.state.db import StateDB
from harness.state.repository import StateRepository


def test_state_store_creates_and_queries_records(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("do work")
    phase_id = repo.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    run_id = repo.create_agent_run(task_id, phase_id, "planner", "planner-1", 0)
    artifact_path = tmp_path / "plan.md"
    artifact_path.write_text("plan", encoding="utf-8")
    ref = ArtifactRef(
        artifact_id=str(uuid.uuid4()),
        task_id=task_id,
        phase_id=phase_id,
        role="planner",
        agent_id="planner-1",
        artifact_type="plan.md",
        path=artifact_path,
        version=1,
        hash="abc",
    )
    repo.create_artifact(ref)
    repo.create_judge_decision(task_id, phase_id, "PLAN_JUDGEMENT", {"decision": "approved"})
    repo.update_agent_run_status(run_id, "COMPLETED")
    repo.update_phase_status(phase_id, "COMPLETED")

    assert repo.get_task(task_id)["status"] == "CREATED"
    assert repo.list_phases(task_id)[0]["phase_type"] == "PLANNING_DRAFT"
    assert repo.list_agent_runs(task_id)[0]["status"] == "COMPLETED"
    assert repo.list_artifacts(task_id)[0]["artifact_type"] == "plan.md"
    assert repo.list_judge_decisions(task_id)[0]["decision_type"] == "PLAN_JUDGEMENT"


def test_state_store_upgrades_existing_tasks_table(tmp_path: Path) -> None:
    db_path = tmp_path / "old_harness.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                user_prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                current_phase TEXT,
                current_role TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    repo = StateRepository(StateDB(db_path))
    task_id = repo.create_task("build something", workflow_type="new_project")

    task = repo.get_task(task_id)
    assert task is not None
    assert task["workflow_type"] == "new_project"
    assert repo.list_tasks(1)[0]["workflow_type"] == "new_project"


def test_repository_task_workflow_and_latest_task_helpers(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    first_task_id = repo.create_task("same prompt")
    second_task_id = repo.create_task("same prompt")

    repo.set_task_workflow_type(first_task_id, "bugfix")

    assert repo.get_task(first_task_id)["workflow_type"] == "bugfix"
    assert repo.latest_task_id("same prompt") == second_task_id
    assert repo.latest_task_id() == second_task_id


def test_state_store_creates_query_indexes(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    repo.create_task("indexed")

    with sqlite3.connect(tmp_path / "harness.db") as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list('artifacts')").fetchall()}

    assert "idx_artifacts_task_type" in indexes
    assert "idx_artifacts_created_at" in indexes


def test_repository_rejects_invalid_status_values(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("state validation")
    phase_id = repo.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    run_id = repo.create_agent_run(task_id, phase_id, "planner", "planner-1", 0)

    with pytest.raises(ValueError, match="Invalid task status"):
        repo.update_task(task_id, status="DONE")
    with pytest.raises(ValueError, match="Invalid phase status"):
        repo.update_phase_status(phase_id, "OUTPUT_INVALID")
    with pytest.raises(ValueError, match="Invalid agent run status"):
        repo.update_agent_run_status(run_id, "SKIPPED")


def test_repository_assigns_artifact_version_inside_insert_lock(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("artifact versions")
    artifact_path = tmp_path / "artifact.md"
    artifact_path.write_text("ok", encoding="utf-8")

    def build_ref(version: int) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=f"artifact-{version}",
            task_id=task_id,
            phase_id=None,
            role="orchestrator",
            agent_id="harness",
            artifact_type="artifact.md",
            path=artifact_path,
            version=version,
            hash="hash",
        )

    first = repo.create_artifact_with_next_version(task_id, "artifact.md", build_ref)
    second = repo.create_artifact_with_next_version(task_id, "artifact.md", build_ref)

    assert (first.version, second.version) == (1, 2)
