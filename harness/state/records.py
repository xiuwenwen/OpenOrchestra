from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any, TypeVar


RecordT = TypeVar("RecordT", bound="RecordMapping")


@dataclass(frozen=True)
class RecordMapping(Mapping[str, Any]):
    @classmethod
    def from_row(cls: type[RecordT], row: Mapping[str, Any]) -> RecordT:
        values = {field.name: row[field.name] for field in fields(cls)}
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __getitem__(self, key: str) -> Any:
        if key not in self.__dataclass_fields__:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.__dataclass_fields__)

    def __len__(self) -> int:
        return len(self.__dataclass_fields__)


@dataclass(frozen=True)
class TaskRecord(RecordMapping):
    task_id: str
    user_prompt: str
    workflow_type: str | None
    status: str
    current_phase: str | None
    current_role: str | None
    configuration: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PhaseRecord(RecordMapping):
    phase_id: str
    task_id: str
    phase_type: str
    role: str
    status: str
    round_id: int | None
    loop_type: str | None
    parent_round_id: int | None
    iteration_id: int | None
    started_at: str
    completed_at: str | None


@dataclass(frozen=True)
class AgentRunRecord(RecordMapping):
    run_id: str
    task_id: str
    phase_id: str
    role: str
    agent_id: str
    status: str
    started_at: str
    completed_at: str | None
    retry_count: int
    error_message: str | None


@dataclass(frozen=True)
class ArtifactRecord(RecordMapping):
    artifact_id: str
    task_id: str
    phase_id: str | None
    role: str | None
    agent_id: str | None
    artifact_type: str
    version: int
    path: str
    hash: str
    created_at: str


@dataclass(frozen=True)
class JudgeDecisionRecord(RecordMapping):
    decision_id: str
    task_id: str
    phase_id: str | None
    decision_type: str
    decision_payload: str
    created_at: str


@dataclass(frozen=True)
class EventRecord(RecordMapping):
    event_id: str
    task_id: str | None
    phase: str | None
    role: str | None
    agent_id: str | None
    round_id: int | None
    attempt: int | None
    event_type: str
    status: str | None
    message: str | None
    trace_id: str | None
    span_id: str | None
    parent_span_id: str | None
    payload: str
    created_at: str
