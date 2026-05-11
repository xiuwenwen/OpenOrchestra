from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.agents.result import ArtifactRef
from harness.core.state_machine import (
    COMPLETED,
    CREATED,
    DELIVERY,
    EXECUTION,
    FAILED,
    FINAL_JUDGEMENT,
    FIXING,
    MISC_RESPONSE,
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
    TEST_JUDGEMENT,
    TESTING,
)
from harness.state.db import StateDB
from harness.state.records import AgentRunRecord, ArtifactRecord, EventRecord, JudgeDecisionRecord, PhaseRecord, TaskRecord


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


_UNSET = object()
PHASE_TYPES = {
    DELIVERY,
    EXECUTION,
    FINAL_JUDGEMENT,
    FIXING,
    MISC_RESPONSE,
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
    TEST_JUDGEMENT,
    TESTING,
}
TASK_STATUSES = {CREATED, RUNNING, COMPLETED, FAILED, "PENDING", *PHASE_TYPES}
PHASE_STATUSES = {RUNNING, COMPLETED, FAILED}
AGENT_RUN_STATUSES = {RUNNING, COMPLETED, FAILED, "OUTPUT_INVALID", "TIMEOUT"}


class StateRepository:
    def __init__(self, db: StateDB):
        self.db = db
        self.db.initialize()
        self._lock = threading.RLock()

    def create_task(self, user_prompt: str, status: str = "CREATED", workflow_type: str | None = None) -> str:
        self._require_status(status, TASK_STATUSES, "task status")
        task_id = str(uuid.uuid4())
        now = utc_now_iso()
        with self._lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, user_prompt, workflow_type, status, current_phase, current_role, configuration, created_at, updated_at)
                VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (task_id, user_prompt, workflow_type, status, now, now),
            )
        return task_id

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return TaskRecord.from_row(row) if row else None

    def list_tasks(self, limit: int = 20) -> list[TaskRecord]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id, user_prompt, workflow_type, status, current_phase, current_role, configuration, created_at, updated_at
                FROM tasks
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [TaskRecord.from_row(row) for row in rows]

    def update_task_configuration(self, task_id: str, configuration: str) -> None:
        now = utc_now_iso()
        with self._lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE tasks SET configuration = ?, updated_at = ? WHERE task_id = ?",
                (configuration, now, task_id),
            )

    def set_task_workflow_type(self, task_id: str, workflow_type: str) -> None:
        now = utc_now_iso()
        with self._lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE tasks SET workflow_type = ?, updated_at = ? WHERE task_id = ?",
                (workflow_type, now, task_id),
            )

    def latest_task_id(self, user_prompt: str | None = None) -> str | None:
        query = "SELECT task_id FROM tasks"
        params: list[Any] = []
        if user_prompt is not None:
            query += " WHERE user_prompt = ?"
            params.append(user_prompt)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self.db.connect() as conn:
            row = conn.execute(query, params).fetchone()
        return row["task_id"] if row else None

    def update_task(
        self,
        task_id: str,
        status: str | object = _UNSET,
        current_phase: str | None | object = _UNSET,
        current_role: str | None | object = _UNSET,
    ) -> None:
        task = self.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        if status is not _UNSET:
            self._require_status(str(status), TASK_STATUSES, "task status")
        with self._lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, current_phase = ?, current_role = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    status if status is not _UNSET else task["status"],
                    current_phase if current_phase is not _UNSET else task["current_phase"],
                    current_role if current_role is not _UNSET else task["current_role"],
                    utc_now_iso(),
                    task_id,
                ),
            )

    def create_phase(self, task_id: str, phase_type: str, role: str, round_id: int, status: str = "RUNNING") -> str:
        self._require_status(status, PHASE_STATUSES, "phase status")
        phase_id = str(uuid.uuid4())
        with self._lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO phases(phase_id, task_id, phase_type, role, status, round_id, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (phase_id, task_id, phase_type, role, status, round_id, utc_now_iso()),
            )
        return phase_id

    def update_phase_status(self, phase_id: str, status: str) -> None:
        self._require_status(status, PHASE_STATUSES, "phase status")
        completed_at = utc_now_iso() if status in {"COMPLETED", "FAILED"} else None
        with self._lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE phases SET status = ?, completed_at = COALESCE(?, completed_at) WHERE phase_id = ?",
                (status, completed_at, phase_id),
            )

    def list_phases(self, task_id: str) -> list[PhaseRecord]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM phases WHERE task_id = ? ORDER BY started_at", (task_id,)).fetchall()
        return [PhaseRecord.from_row(row) for row in rows]

    def create_agent_run(
        self,
        task_id: str,
        phase_id: str,
        role: str,
        agent_id: str,
        retry_count: int,
        status: str = "RUNNING",
    ) -> str:
        self._require_status(status, AGENT_RUN_STATUSES, "agent run status")
        run_id = str(uuid.uuid4())
        with self._lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs(run_id, task_id, phase_id, role, agent_id, status, started_at, completed_at, retry_count, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)
                """,
                (run_id, task_id, phase_id, role, agent_id, status, utc_now_iso(), retry_count),
            )
        return run_id

    def update_agent_run_status(self, run_id: str, status: str, error_message: str | None = None) -> None:
        self._require_status(status, AGENT_RUN_STATUSES, "agent run status")
        completed_at = utc_now_iso() if status in {"COMPLETED", "FAILED", "OUTPUT_INVALID", "TIMEOUT"} else None
        with self._lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE agent_runs
                SET status = ?, completed_at = COALESCE(?, completed_at), error_message = ?
                WHERE run_id = ?
                """,
                (status, completed_at, error_message, run_id),
            )

    def list_agent_runs(self, task_id: str) -> list[AgentRunRecord]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM agent_runs WHERE task_id = ? ORDER BY started_at", (task_id,)).fetchall()
        return [AgentRunRecord.from_row(row) for row in rows]

    def next_artifact_version(self, task_id: str, artifact_type: str) -> int:
        with self._lock, self.db.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS max_version FROM artifacts WHERE task_id = ? AND artifact_type = ?",
                (task_id, artifact_type),
            ).fetchone()
        max_version = row["max_version"] if row else None
        return int(max_version or 0) + 1

    def create_artifact(self, ref: ArtifactRef) -> None:
        with self._lock, self.db.connect() as conn:
            self._insert_artifact(conn, ref)

    def create_artifact_with_next_version(
        self,
        task_id: str,
        artifact_type: str,
        ref_factory: Callable[[int], ArtifactRef],
    ) -> ArtifactRef:
        with self._lock, self.db.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS max_version FROM artifacts WHERE task_id = ? AND artifact_type = ?",
                (task_id, artifact_type),
            ).fetchone()
            version = int((row["max_version"] if row else None) or 0) + 1
            ref = ref_factory(version)
            if ref.task_id != task_id:
                raise ValueError(f"Artifact task_id mismatch: {ref.task_id!r} != {task_id!r}")
            if ref.artifact_type != artifact_type:
                raise ValueError(f"Artifact type mismatch: {ref.artifact_type!r} != {artifact_type!r}")
            if ref.version != version:
                raise ValueError(f"Artifact version mismatch: {ref.version!r} != {version!r}")
            self._insert_artifact(conn, ref)
            return ref

    def list_artifacts(self, task_id: str, artifact_type: str | None = None) -> list[ArtifactRecord]:
        query = "SELECT * FROM artifacts WHERE task_id = ?"
        params: list[Any] = [task_id]
        if artifact_type:
            query += " AND artifact_type = ?"
            params.append(artifact_type)
        query += " ORDER BY created_at, version"
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [ArtifactRecord.from_row(row) for row in rows]

    def _insert_artifact(self, conn: Any, ref: ArtifactRef) -> None:
        conn.execute(
            """
            INSERT INTO artifacts(artifact_id, task_id, phase_id, role, agent_id, artifact_type, version, path, hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref.artifact_id,
                ref.task_id,
                ref.phase_id,
                ref.role,
                ref.agent_id,
                ref.artifact_type,
                ref.version,
                str(ref.path),
                ref.hash,
                utc_now_iso(),
            ),
        )

    def create_judge_decision(self, task_id: str, phase_id: str | None, decision_type: str, payload: dict[str, Any]) -> str:
        decision_id = str(uuid.uuid4())
        with self._lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO judge_decisions(decision_id, task_id, phase_id, decision_type, decision_payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (decision_id, task_id, phase_id, decision_type, json.dumps(payload, ensure_ascii=False), utc_now_iso()),
            )
        return decision_id

    def list_judge_decisions(self, task_id: str) -> list[JudgeDecisionRecord]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM judge_decisions WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        return [JudgeDecisionRecord.from_row(row) for row in rows]

    def record_event(
        self,
        *,
        event_type: str,
        task_id: str | None = None,
        phase: str | None = None,
        role: str | None = None,
        agent_id: str | None = None,
        round_id: int | None = None,
        attempt: int | None = None,
        status: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        with self._lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO events(event_id, task_id, phase, role, agent_id, round_id, attempt, event_type, status, message, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    task_id,
                    phase,
                    role,
                    agent_id,
                    round_id,
                    attempt,
                    event_type,
                    status,
                    message,
                    json.dumps(payload or {}, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )
        return event_id

    def list_events(self, task_id: str | None = None, limit: int = 200) -> list[EventRecord]:
        query = "SELECT * FROM events"
        params: list[Any] = []
        if task_id is not None:
            query += " WHERE task_id = ?"
            params.append(task_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [EventRecord.from_row(row) for row in rows]

    def _require_status(self, status: str, allowed: set[str], label: str) -> None:
        if status not in allowed:
            allowed_values = ", ".join(sorted(allowed))
            raise ValueError(f"Invalid {label}: {status!r}; expected one of: {allowed_values}")
