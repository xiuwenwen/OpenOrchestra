from __future__ import annotations

import pytest

from harness.events import EventEnvelope, InMemoryEventStore, SQLiteEventStore, TraceContext


def test_event_envelope_requires_trace_and_correlation_ids() -> None:
    with pytest.raises(ValueError, match="trace_id is required"):
        EventEnvelope(event_type="TaskCreated", trace_id="", correlation_id="c1")

    with pytest.raises(ValueError, match="correlation_id is required"):
        EventEnvelope(event_type="TaskCreated", trace_id="t1", correlation_id="")


def test_event_store_is_append_only_and_replayable() -> None:
    trace = TraceContext.start(trace_id="trace-1")
    store = InMemoryEventStore()
    task_created = EventEnvelope.create(
        "TaskCreated",
        task_id="task-1",
        trace=trace,
        payload={"prompt": "fix bug"},
        aggregate_type="task",
        aggregate_id="task-1",
    )
    phase_requested = EventEnvelope.create(
        "PhaseRequested",
        task_id="task-1",
        trace=trace.child("phase-plan"),
        payload={"phase": "planning"},
        aggregate_type="phase",
        aggregate_id="phase-1",
    )

    store.append_many((task_created, phase_requested))

    assert store.replay("task-1") == (task_created, phase_requested)
    assert store.stream(event_type="PhaseRequested") == (phase_requested,)
    with pytest.raises(ValueError, match="duplicate event_id"):
        store.append(task_created)


def test_event_payload_is_immutable_after_creation() -> None:
    trace = TraceContext.start(trace_id="trace-2")
    event = EventEnvelope.create("TaskCreated", trace=trace, payload={"status": "created"})

    with pytest.raises(TypeError):
        event.payload["status"] = "mutated"


def test_sqlite_event_store_persists_and_replays_across_instances(tmp_path) -> None:
    db_path = tmp_path / "events.sqlite3"
    trace = TraceContext.start(trace_id="trace-sqlite")
    event = EventEnvelope.create(
        "TaskCreated",
        task_id="task-sqlite",
        trace=trace,
        payload={"prompt": "fix persisted bug"},
        aggregate_type="task",
        aggregate_id="task-sqlite",
    )

    SQLiteEventStore(db_path).append(event)
    reopened = SQLiteEventStore(db_path)

    assert reopened.replay("task-sqlite")[0].to_dict() == event.to_dict()
    with pytest.raises(ValueError, match="duplicate event_id"):
        reopened.append(event)
