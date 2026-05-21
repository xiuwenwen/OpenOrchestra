from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from harness.events import EventEnvelope


@dataclass(frozen=True)
class ReplayRouteExpectation:
    step_name: str
    event_type: str
    target_step: str
    route_action: str


@dataclass(frozen=True)
class ReplayFixture:
    name: str
    task_id: str
    events: tuple[EventEnvelope, ...]
    route_expectations: tuple[ReplayRouteExpectation, ...]


def load_replay_fixture(path: Path) -> ReplayFixture:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one JSON object")
    events_payload = payload.get("events")
    if not isinstance(events_payload, list):
        raise ValueError(f"{path}.events must be a list")
    expectations_payload = payload.get("route_expectations")
    if not isinstance(expectations_payload, list):
        raise ValueError(f"{path}.route_expectations must be a list")
    return ReplayFixture(
        name=_required_string(payload, "name", path),
        task_id=_required_string(payload, "task_id", path),
        events=tuple(_event_from_payload(event, path) for event in events_payload),
        route_expectations=tuple(_expectation_from_payload(item, path) for item in expectations_payload),
    )


def _event_from_payload(payload: Any, path: Path) -> EventEnvelope:
    if not isinstance(payload, dict):
        raise ValueError(f"{path}.events entries must be objects")
    return EventEnvelope(
        event_id=_required_string(payload, "event_id", path),
        event_type=_required_string(payload, "event_type", path),
        schema_version=int(payload.get("schema_version", 1)),
        task_id=_optional_string(payload.get("task_id")),
        aggregate_type=_optional_string(payload.get("aggregate_type")),
        aggregate_id=_optional_string(payload.get("aggregate_id")),
        trace_id=_required_string(payload, "trace_id", path),
        correlation_id=_required_string(payload, "correlation_id", path),
        span_id=_optional_string(payload.get("span_id")),
        parent_span_id=_optional_string(payload.get("parent_span_id")),
        created_at=_required_string(payload, "created_at", path),
        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
    )


def _expectation_from_payload(payload: Any, path: Path) -> ReplayRouteExpectation:
    if not isinstance(payload, dict):
        raise ValueError(f"{path}.route_expectations entries must be objects")
    return ReplayRouteExpectation(
        step_name=_required_string(payload, "step_name", path),
        event_type=_required_string(payload, "event_type", path),
        target_step=_required_string(payload, "target_step", path),
        route_action=_required_string(payload, "route_action", path),
    )


def _required_string(payload: dict[str, Any], field: str, path: Path) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}.{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
