from __future__ import annotations

from dataclasses import dataclass

from harness.core.state_machine import (
    COMPLETED,
    CREATED,
    DELIVERY,
    EXECUTION,
    FAILED,
    FIXING,
    MISC_RESPONSE,
    PATCH_MERGE,
    PLAN_REVIEW,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEWING,
    RUNNING,
    TESTING,
)


PENDING = "PENDING"
OUTPUT_INVALID = "OUTPUT_INVALID"
TIMEOUT = "TIMEOUT"
PHASE_TYPES = {
    DELIVERY,
    EXECUTION,
    FIXING,
    MISC_RESPONSE,
    PATCH_MERGE,
    PLAN_REVIEW,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEWING,
    TESTING,
}
TASK_STATUSES = {CREATED, RUNNING, COMPLETED, FAILED, PENDING, *PHASE_TYPES}
PHASE_STATUSES = {RUNNING, COMPLETED, FAILED}
AGENT_RUN_STATUSES = {RUNNING, COMPLETED, FAILED, OUTPUT_INVALID, TIMEOUT}


def _task_transitions() -> dict[str, frozenset[str]]:
    active = frozenset({RUNNING, *PHASE_TYPES})
    transitions = {
        CREATED: frozenset({CREATED, RUNNING, COMPLETED, FAILED, *PHASE_TYPES}),
        PENDING: frozenset({PENDING, CREATED, RUNNING, FAILED, *PHASE_TYPES}),
        RUNNING: frozenset({RUNNING, COMPLETED, FAILED, *PHASE_TYPES}),
        COMPLETED: frozenset({COMPLETED}),
        FAILED: frozenset({FAILED, RUNNING, *PHASE_TYPES}),
    }
    for phase_type in PHASE_TYPES:
        transitions[phase_type] = frozenset({COMPLETED, FAILED, *active})
    return transitions


@dataclass(frozen=True)
class TransitionTable:
    label: str
    allowed_statuses: frozenset[str]
    transitions: dict[str, frozenset[str]]

    def validate_initial(self, status: str) -> None:
        self.validate_status(status)

    def validate_transition(self, current: str, target: str) -> None:
        self.validate_status(target)
        self.validate_status(current)
        allowed_targets = self.transitions.get(current, frozenset())
        if target not in allowed_targets:
            allowed_values = ", ".join(sorted(allowed_targets))
            raise ValueError(
                f"Invalid {self.label} status transition: {current!r} -> {target!r}; "
                f"expected one of: {allowed_values}"
            )

    def validate_status(self, status: str) -> None:
        if status not in self.allowed_statuses:
            allowed_values = ", ".join(sorted(self.allowed_statuses))
            raise ValueError(f"Invalid {self.label} status: {status!r}; expected one of: {allowed_values}")


TASK_TRANSITIONS = TransitionTable(
    "task",
    frozenset(TASK_STATUSES),
    _task_transitions(),
)
PHASE_TRANSITIONS = TransitionTable(
    "phase",
    frozenset(PHASE_STATUSES),
    {
        RUNNING: frozenset({RUNNING, COMPLETED, FAILED}),
        COMPLETED: frozenset({COMPLETED}),
        FAILED: frozenset({FAILED, COMPLETED}),
    },
)
AGENT_RUN_TRANSITIONS = TransitionTable(
    "agent run",
    frozenset(AGENT_RUN_STATUSES),
    {
        RUNNING: frozenset({RUNNING, COMPLETED, FAILED, OUTPUT_INVALID, TIMEOUT}),
        COMPLETED: frozenset({COMPLETED}),
        FAILED: frozenset({FAILED}),
        OUTPUT_INVALID: frozenset({OUTPUT_INVALID}),
        TIMEOUT: frozenset({TIMEOUT}),
    },
)
