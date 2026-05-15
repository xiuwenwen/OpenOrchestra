from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from harness.artifacts.review_decision import review_decision_code_from_payload
from harness.testing.tester_result import TesterResult


class WorkflowErrorType(str, Enum):
    NONE = "none"
    SOURCE_BUG = "source_bug"
    ENVIRONMENT_ISSUE = "environment_issue"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    REVIEW_BLOCKED = "review_blocked"
    ARTIFACT_INVALID = "artifact_invalid"
    PATCH_APPLY_ERROR = "patch_apply_error"


class WorkflowRouteAction(str, Enum):
    CONTINUE = "continue"
    EXECUTOR_FIX = "executor_fix"
    TESTER_ENVIRONMENT_REPAIR = "tester_environment_repair"
    BLOCK_TASK = "block_task"
    RETRY_ARTIFACT = "retry_artifact"


@dataclass(frozen=True)
class WorkflowRoute:
    action: WorkflowRouteAction
    error_type: WorkflowErrorType
    reason: str = ""


READY_ENVIRONMENT_STATUSES = {"ready", "not_applicable", "pass", "passed", "success"}


def route_tester_decision(decision: TesterResult) -> WorkflowRoute:
    if decision.has_environment_dependency_issue:
        return WorkflowRoute(
            WorkflowRouteAction.TESTER_ENVIRONMENT_REPAIR,
            WorkflowErrorType.ENVIRONMENT_ISSUE,
            decision.summary or decision.failure_type or "tester reported environment dependency issue",
        )
    if decision.tests_passed:
        return WorkflowRoute(WorkflowRouteAction.CONTINUE, WorkflowErrorType.NONE, decision.summary)
    if decision.source_bug:
        return WorkflowRoute(
            WorkflowRouteAction.EXECUTOR_FIX,
            WorkflowErrorType.SOURCE_BUG,
            decision.summary or decision.failure_type or "tester reported source bug",
        )
    if decision.environment_blocked:
        return WorkflowRoute(
            WorkflowRouteAction.BLOCK_TASK,
            WorkflowErrorType.ENVIRONMENT_BLOCKED,
            decision.summary or decision.failure_type or "tester reported environment blocked",
        )
    return WorkflowRoute(
        WorkflowRouteAction.RETRY_ARTIFACT,
        WorkflowErrorType.ARTIFACT_INVALID,
        f"unrecognized tester status: {decision.status}",
    )


def route_review_payload(payload: dict[str, Any]) -> WorkflowRoute:
    decision_code = review_decision_code_from_payload(payload)
    environment_check = payload.get("environment_check")
    environment_status = ""
    blocking_reason = ""
    if isinstance(environment_check, dict):
        environment_status = _normalized(environment_check.get("status"))
        blocking_reason = _string(environment_check.get("blocking_reason"))

    summary = _string(payload.get("summary") or payload.get("reason"))
    if environment_status == "blocked":
        return WorkflowRoute(
            WorkflowRouteAction.BLOCK_TASK,
            WorkflowErrorType.ENVIRONMENT_BLOCKED,
            blocking_reason or summary or "reviewer reported a blocked runtime environment",
        )
    if decision_code == 2:
        return WorkflowRoute(
            WorkflowRouteAction.BLOCK_TASK,
            WorkflowErrorType.REVIEW_BLOCKED,
            blocking_reason or summary or "reviewer blocked the workflow",
        )
    if decision_code == 1:
        return WorkflowRoute(
            WorkflowRouteAction.EXECUTOR_FIX,
            WorkflowErrorType.SOURCE_BUG,
            summary or "reviewer requested source changes",
        )
    if decision_code == 0:
        if environment_status in READY_ENVIRONMENT_STATUSES:
            return WorkflowRoute(WorkflowRouteAction.CONTINUE, WorkflowErrorType.NONE, summary)
        if environment_status == "changes_required":
            return WorkflowRoute(
                WorkflowRouteAction.TESTER_ENVIRONMENT_REPAIR,
                WorkflowErrorType.ENVIRONMENT_ISSUE,
                blocking_reason or summary or "reviewer requested environment follow-up",
            )
        return WorkflowRoute(
            WorkflowRouteAction.RETRY_ARTIFACT,
            WorkflowErrorType.ARTIFACT_INVALID,
            f"review_result.json has approved source verdict but invalid environment status: {environment_status or '<missing>'}",
        )
    return WorkflowRoute(
        WorkflowRouteAction.RETRY_ARTIFACT,
        WorkflowErrorType.ARTIFACT_INVALID,
        "review_result.json does not contain a routeable review verdict",
    )


def choose_review_route(routes: list[WorkflowRoute]) -> WorkflowRoute:
    if not routes:
        return WorkflowRoute(
            WorkflowRouteAction.RETRY_ARTIFACT,
            WorkflowErrorType.ARTIFACT_INVALID,
            "missing review_result.json",
        )
    for action in (
        WorkflowRouteAction.BLOCK_TASK,
        WorkflowRouteAction.EXECUTOR_FIX,
        WorkflowRouteAction.TESTER_ENVIRONMENT_REPAIR,
        WorkflowRouteAction.RETRY_ARTIFACT,
        WorkflowRouteAction.CONTINUE,
    ):
        for route in routes:
            if route.action == action:
                return route
    return routes[-1]


def _normalized(value: Any) -> str:
    return _string(value).lower()


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()
