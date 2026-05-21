from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from harness.events.model import EventEnvelope, TraceContext


class ArtifactDecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class RawArtifact:
    artifact_id: str
    task_id: str
    artifact_type: str
    producer: str
    path: Path
    content_hash: str

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id is required")
        if not self.task_id:
            raise ValueError("task_id is required")
        if not self.artifact_type:
            raise ValueError("artifact_type is required")
        if not self.producer:
            raise ValueError("producer is required")


@dataclass(frozen=True)
class CanonicalArtifact:
    artifact_id: str
    raw_artifact_id: str
    artifact_type: str
    path: Path
    content_hash: str

    def __post_init__(self) -> None:
        if self.artifact_id == self.raw_artifact_id:
            raise ValueError("canonical artifact must not reuse raw artifact id")


@dataclass(frozen=True)
class CanonicalizationChange:
    field_path: str
    before: Any
    after: Any
    rule_name: str

    def __post_init__(self) -> None:
        if not self.field_path:
            raise ValueError("field_path is required")
        if not self.rule_name:
            raise ValueError("rule_name is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_path": self.field_path,
            "before": self.before,
            "after": self.after,
            "rule_name": self.rule_name,
        }


@dataclass(frozen=True)
class CanonicalizationReport:
    raw_artifact: RawArtifact
    canonical_artifact: CanonicalArtifact | None
    changes: tuple[CanonicalizationChange, ...] = ()
    rejection_reasons: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.canonical_artifact is not None and not self.rejection_reasons

    def __post_init__(self) -> None:
        if self.canonical_artifact is None and not self.rejection_reasons:
            raise ValueError("rejected reports must include rejection_reasons")
        if self.canonical_artifact is not None and self.canonical_artifact.raw_artifact_id != self.raw_artifact.artifact_id:
            raise ValueError("canonical artifact must reference raw artifact")


@dataclass(frozen=True)
class ArtifactDecision:
    status: ArtifactDecisionStatus
    raw_artifact_id: str
    canonical_artifact_id: str | None = None
    reasons: tuple[str, ...] = ()
    changes: tuple[CanonicalizationChange, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.status == ArtifactDecisionStatus.ACCEPTED and not self.canonical_artifact_id:
            raise ValueError("accepted artifact decisions require canonical_artifact_id")
        if self.status == ArtifactDecisionStatus.REJECTED and not self.reasons:
            raise ValueError("rejected artifact decisions require reasons")


def canonicalization_event(report: CanonicalizationReport, *, trace: TraceContext) -> EventEnvelope:
    event_type = "ArtifactCanonicalized" if report.accepted else "ArtifactRejected"
    payload: dict[str, Any] = {
        "raw_artifact_id": report.raw_artifact.artifact_id,
        "artifact_type": report.raw_artifact.artifact_type,
        "changes": [change.to_dict() for change in report.changes],
        "rejection_reasons": list(report.rejection_reasons),
    }
    if report.canonical_artifact is not None:
        payload["canonical_artifact_id"] = report.canonical_artifact.artifact_id
    return EventEnvelope.create(
        event_type,
        trace=trace,
        task_id=report.raw_artifact.task_id,
        aggregate_type="artifact",
        aggregate_id=report.raw_artifact.artifact_id,
        payload=payload,
    )
