from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterator, Protocol

from harness.events.model import EventEnvelope


class EventStore(Protocol):
    def append(self, event: EventEnvelope) -> EventEnvelope:
        ...

    def append_many(self, events: Iterable[EventEnvelope]) -> tuple[EventEnvelope, ...]:
        ...

    def stream(
        self,
        *,
        task_id: str | None = None,
        event_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> tuple[EventEnvelope, ...]:
        ...

    def replay(self, task_id: str) -> tuple[EventEnvelope, ...]:
        ...


class InMemoryEventStore:
    """Append-only event store used by V2 unit tests and replay scaffolding."""

    def __init__(self) -> None:
        self._events: list[EventEnvelope] = []
        self._event_ids: set[str] = set()
        self._lock = threading.RLock()

    def append(self, event: EventEnvelope) -> EventEnvelope:
        with self._lock:
            if event.event_id in self._event_ids:
                raise ValueError(f"duplicate event_id: {event.event_id}")
            self._events.append(event)
            self._event_ids.add(event.event_id)
        return event

    def append_many(self, events: Iterable[EventEnvelope]) -> tuple[EventEnvelope, ...]:
        return tuple(self.append(event) for event in events)

    def stream(
        self,
        *,
        task_id: str | None = None,
        event_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> tuple[EventEnvelope, ...]:
        with self._lock:
            events = tuple(self._events)
        if task_id is not None:
            events = tuple(event for event in events if event.task_id == task_id)
        if event_type is not None:
            events = tuple(event for event in events if event.event_type == event_type)
        if aggregate_id is not None:
            events = tuple(event for event in events if event.aggregate_id == aggregate_id)
        return events

    def replay(self, task_id: str) -> tuple[EventEnvelope, ...]:
        return self.stream(task_id=task_id)


class SQLiteEventStore:
    """Durable append-only event store for V2 workflow replay."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_schema()

    def append(self, event: EventEnvelope) -> EventEnvelope:
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO harness_events (
                        event_id,
                        event_type,
                        schema_version,
                        task_id,
                        aggregate_type,
                        aggregate_id,
                        trace_id,
                        correlation_id,
                        span_id,
                        parent_span_id,
                        created_at,
                        payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.schema_version,
                        event.task_id,
                        event.aggregate_type,
                        event.aggregate_id,
                        event.trace_id,
                        event.correlation_id,
                        event.span_id,
                        event.parent_span_id,
                        event.created_at,
                        json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"duplicate event_id: {event.event_id}") from exc
        return event

    def append_many(self, events: Iterable[EventEnvelope]) -> tuple[EventEnvelope, ...]:
        appended: list[EventEnvelope] = []
        with self._lock, self._connect() as connection:
            try:
                for event in events:
                    connection.execute(
                        """
                        INSERT INTO harness_events (
                            event_id,
                            event_type,
                            schema_version,
                            task_id,
                            aggregate_type,
                            aggregate_id,
                            trace_id,
                            correlation_id,
                            span_id,
                            parent_span_id,
                            created_at,
                            payload_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            event.event_type,
                            event.schema_version,
                            event.task_id,
                            event.aggregate_type,
                            event.aggregate_id,
                            event.trace_id,
                            event.correlation_id,
                            event.span_id,
                            event.parent_span_id,
                            event.created_at,
                            json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
                        ),
                    )
                    appended.append(event)
            except sqlite3.IntegrityError as exc:
                raise ValueError("duplicate event_id in event batch") from exc
        return tuple(appended)

    def stream(
        self,
        *,
        task_id: str | None = None,
        event_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> tuple[EventEnvelope, ...]:
        clauses: list[str] = []
        values: list[str] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            values.append(task_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            values.append(event_type)
        if aggregate_id is not None:
            clauses.append("aggregate_id = ?")
            values.append(aggregate_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM harness_events {where} ORDER BY sequence ASC",
                values,
            ).fetchall()
        return tuple(self._row_to_event(row) for row in rows)

    def replay(self, task_id: str) -> tuple[EventEnvelope, ...]:
        return self.stream(task_id=task_id)

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS harness_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    task_id TEXT,
                    aggregate_type TEXT,
                    aggregate_id TEXT,
                    trace_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    span_id TEXT,
                    parent_span_id TEXT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_harness_events_task_sequence ON harness_events(task_id, sequence)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_harness_events_type_sequence ON harness_events(event_type, sequence)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_harness_events_aggregate_sequence ON harness_events(aggregate_id, sequence)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _row_to_event(self, row: sqlite3.Row) -> EventEnvelope:
        return EventEnvelope(
            event_id=str(row["event_id"]),
            event_type=str(row["event_type"]),
            schema_version=int(row["schema_version"]),
            task_id=row["task_id"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            trace_id=str(row["trace_id"]),
            correlation_id=str(row["correlation_id"]),
            span_id=row["span_id"],
            parent_span_id=row["parent_span_id"],
            created_at=str(row["created_at"]),
            payload=json.loads(str(row["payload_json"])),
        )
