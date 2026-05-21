from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Mapping
import uuid


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    correlation_id: str
    span_id: str | None = None
    parent_span_id: str | None = None

    @classmethod
    def start(cls, *, trace_id: str | None = None, correlation_id: str | None = None) -> "TraceContext":
        root = trace_id or str(uuid.uuid4())
        return cls(trace_id=root, correlation_id=correlation_id or root, span_id=root)

    def child(self, span_id: str | None = None) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            correlation_id=self.correlation_id,
            span_id=span_id or str(uuid.uuid4()),
            parent_span_id=self.span_id,
        )


@dataclass(frozen=True)
class EventEnvelope:
    event_type: str
    trace_id: str
    correlation_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str | None = None
    aggregate_type: str | None = None
    aggregate_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not self.event_type:
            raise ValueError("event_type is required")
        if not self.trace_id:
            raise ValueError("trace_id is required")
        if not self.correlation_id:
            raise ValueError("correlation_id is required")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def create(
        cls,
        event_type: str,
        *,
        trace: TraceContext,
        payload: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> "EventEnvelope":
        return cls(
            event_type=event_type,
            task_id=task_id,
            trace_id=trace.trace_id,
            correlation_id=trace.correlation_id,
            span_id=trace.span_id,
            parent_span_id=trace.parent_span_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "created_at": self.created_at,
            "payload": dict(self.payload),
        }
