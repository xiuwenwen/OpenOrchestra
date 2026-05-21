from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class PhaseStatus(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class DecisionType(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_RETRY = "needs_retry"
    BLOCKED = "blocked"


class FailureType(StrEnum):
    NONE = "none"
    SOURCE_BUG = "source_bug"
    ENVIRONMENT = "environment"
    CONTRACT = "contract"
    RUNTIME = "runtime"
    INFRA = "infra"
    INCONCLUSIVE = "inconclusive"


class RouteAction(StrEnum):
    CONTINUE = "continue"
    RETRY_AGENT = "retry_agent"
    FIX_SOURCE = "fix_source"
    REPAIR_ENVIRONMENT = "repair_environment"
    RETEST_CURRENT_REPO_SNAPSHOT = "retest_current_repo_snapshot"
    BLOCK_TASK = "block_task"
    FAIL_TASK = "fail_task"


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Decision:
    decision_type: DecisionType
    route_action: RouteAction
    failure_type: FailureType = FailureType.NONE
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.failure_type != FailureType.NONE and not self.reason:
            raise ValueError("non-success decisions must include a reason")


@dataclass(frozen=True)
class GateResult:
    gate_name: str
    status: GateStatus
    failure_type: FailureType = FailureType.NONE
    evidence_refs: tuple[str, ...] = ()
    next_recommended_action: RouteAction = RouteAction.CONTINUE
    summary: str = ""

    def __post_init__(self) -> None:
        if not self.gate_name:
            raise ValueError("gate_name is required")
        if self.status != GateStatus.PASSED and self.failure_type == FailureType.NONE:
            raise ValueError("non-passing gate results must classify failure_type")


@dataclass(frozen=True)
class RepoSnapshot:
    snapshot_id: str
    task_id: str
    root: Path
    source: str
    parent_snapshot_id: str | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            raise ValueError("snapshot_id is required")
        if not self.task_id:
            raise ValueError("task_id is required")
        if not self.source:
            raise ValueError("source is required")
