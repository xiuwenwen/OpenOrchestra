from __future__ import annotations

import uuid
import sqlite3
from pathlib import Path

import pytest

from harness.agents.result import ArtifactRef
from harness.adapters.health import BackendHealthSnapshot
from harness.state.db import StateDB
from harness.state.records import AgentRunRecord, ArtifactRecord, EventRecord, PhaseRecord, TaskRecord
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
    repo.update_agent_run_status(run_id, "COMPLETED")
    repo.update_phase_status(phase_id, "COMPLETED")

    assert repo.get_task(task_id)["status"] == "CREATED"
    assert repo.list_phases(task_id)[0]["phase_type"] == "PLANNING_DRAFT"
    assert repo.list_agent_runs(task_id)[0]["status"] == "COMPLETED"
    assert repo.list_artifacts(task_id)[0]["artifact_type"] == "plan.md"


def test_repository_returns_typed_records_with_mapping_compatibility(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("typed")
    phase_id = repo.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    run_id = repo.create_agent_run(task_id, phase_id, "planner", "planner-1", 0)
    artifact_path = tmp_path / "plan.md"
    artifact_path.write_text("plan", encoding="utf-8")
    repo.create_artifact(
        ArtifactRef(
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
    )
    repo.record_event(event_type="phase_started", task_id=task_id, phase="PLANNING_DRAFT")

    task = repo.get_task(task_id)
    phase = repo.list_phases(task_id)[0]
    run = repo.list_agent_runs(task_id)[0]
    artifact = repo.list_artifacts(task_id)[0]
    event = repo.list_events(task_id)[0]

    assert isinstance(task, TaskRecord)
    assert isinstance(phase, PhaseRecord)
    assert isinstance(run, AgentRunRecord)
    assert isinstance(artifact, ArtifactRecord)
    assert isinstance(event, EventRecord)
    assert task["task_id"] == task_id
    assert task.get("status") == "CREATED"
    assert dict(artifact)["artifact_type"] == "plan.md"
    assert task.to_dict()["user_prompt"] == "typed"


def test_phase_records_include_structured_loop_metadata(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("loop metadata")
    repo.create_phase(
        task_id,
        "REGRESSION_TESTING",
        "tester",
        2,
        loop_type="regression_test_fix",
        parent_round_id=1,
        iteration_id=1,
    )

    phase = repo.list_phases(task_id)[0]

    assert phase["round_id"] == 2
    assert phase["loop_type"] == "regression_test_fix"
    assert phase["parent_round_id"] == 1
    assert phase["iteration_id"] == 1


def test_repository_row_conversion_goes_through_typed_records() -> None:
    source = Path("harness/state/repository.py").read_text(encoding="utf-8")

    assert "dict(row)" not in source
    assert "from_row(row)" in source


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


def test_state_store_upgrades_existing_phases_table_with_loop_metadata(tmp_path: Path) -> None:
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
        conn.execute(
            """
            CREATE TABLE phases (
                phase_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                phase_type TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                round_id INTEGER DEFAULT 0,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )

    repo = StateRepository(StateDB(db_path))
    task_id = repo.create_task("upgrade phases")
    repo.create_phase(task_id, "REGRESSION_TESTING", "tester", 1, loop_type="regression_test_fix")

    assert repo.list_phases(task_id)[0]["loop_type"] == "regression_test_fix"


def test_state_store_upgrades_existing_events_table_with_trace_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "old_events.db"
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
        conn.execute(
            """
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                task_id TEXT,
                phase TEXT,
                role TEXT,
                agent_id TEXT,
                round_id INTEGER,
                attempt INTEGER,
                event_type TEXT NOT NULL,
                status TEXT,
                message TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    repo = StateRepository(StateDB(db_path))
    task_id = repo.create_task("upgrade events")
    repo.record_event(event_type="task_started", task_id=task_id, trace_id=task_id, span_id="task_started")

    event = repo.list_events(task_id)[0]
    assert event["trace_id"] == task_id
    assert event["span_id"] == "task_started"


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


def test_repository_enforces_phase_and_agent_transition_tables(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("transition validation")
    phase_id = repo.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    run_id = repo.create_agent_run(task_id, phase_id, "planner", "planner-1", 0)

    repo.update_agent_run_status(run_id, "COMPLETED")
    repo.update_phase_status(phase_id, "COMPLETED")

    with pytest.raises(ValueError, match="Invalid phase status transition"):
        repo.update_phase_status(phase_id, "FAILED")
    with pytest.raises(ValueError, match="Invalid agent run status transition"):
        repo.update_agent_run_status(run_id, "FAILED")


def test_repository_allows_explicit_checkpoint_recovery_transition(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("checkpoint recovery")
    phase_id = repo.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)

    repo.update_phase_status(phase_id, "FAILED")
    repo.update_phase_status(phase_id, "COMPLETED")

    assert repo.list_phases(task_id)[0]["status"] == "COMPLETED"


def test_repository_enforces_task_transition_table(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("task transition")

    repo.update_task(task_id, status="RUNNING")
    repo.update_task(task_id, status="COMPLETED")

    with pytest.raises(ValueError, match="Invalid task status transition"):
        repo.update_task(task_id, status="RUNNING")


def test_repository_can_explicitly_reopen_completed_task_for_followup(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("task transition")

    repo.update_task(task_id, status="RUNNING")
    repo.update_task(task_id, status="COMPLETED")
    repo.reopen_task_for_followup(task_id)

    task = repo.get_task(task_id)
    assert task["status"] == "RUNNING"
    assert task["current_phase"] == "RUNNING"


def test_repository_appends_followup_prompt_turns(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("Build a weather app")

    repo.append_task_prompt_turn(task_id, "Add export")
    repo.append_task_prompt_turn(task_id, "Add export")

    prompt = repo.get_task(task_id)["user_prompt"]
    assert prompt == "Build a weather app\n\nFollow-up request:\nAdd export"
    assert repo.get_task(task_id)["prompt_turn_id"] == 1


def test_repository_tags_phases_with_current_prompt_turn(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("Build a weather app")
    first_phase = repo.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)

    repo.append_task_prompt_turn(task_id, "Add export")
    second_phase = repo.create_phase(task_id, "FIXING", "executor", 1)

    phases = repo.list_phases(task_id)
    assert phases[0]["phase_id"] == first_phase
    assert phases[0]["prompt_turn_id"] == 0
    assert phases[1]["phase_id"] == second_phase
    assert phases[1]["prompt_turn_id"] == 1


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


def test_state_store_records_append_only_events(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))
    task_id = repo.create_task("eventful")

    event_id = repo.record_event(
        event_type="phase_started",
        task_id=task_id,
        phase="TESTING",
        role="tester",
        status="RUNNING",
        trace_id=task_id,
        span_id="phase:TESTING:round-1",
        parent_span_id=f"task:{task_id}",
        payload={"round_id": 1},
    )

    events = repo.list_events(task_id)
    assert events[0]["event_id"] == event_id
    assert events[0]["event_type"] == "phase_started"
    assert events[0]["trace_id"] == task_id
    assert events[0]["span_id"] == "phase:TESTING:round-1"
    assert events[0]["parent_span_id"] == f"task:{task_id}"
    assert events[0]["payload"] == '{"round_id": 1}'


def test_state_store_persists_backend_health_state(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "harness.db"))

    repo.save_backend_health_state(
        BackendHealthSnapshot(
            backend="claude",
            state="open",
            allowed=False,
            consecutive_failures=2,
            failure_kind="timeout",
            open_until=123.5,
            reason="cooling down",
        )
    )

    states = repo.load_backend_health_states()

    assert states["claude"] == {
        "state": "open",
        "consecutive_failures": 2,
        "failure_kind": "timeout",
        "open_until": 123.5,
        "reason": "cooling down",
    }
