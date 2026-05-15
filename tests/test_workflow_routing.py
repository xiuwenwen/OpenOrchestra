from __future__ import annotations

from pathlib import Path

import pytest

from harness.testing.tester_result import TesterResult as HarnessTesterResult
from harness.workflow.routing import (
    WorkflowErrorType,
    WorkflowRouteAction,
    route_review_payload,
    route_tester_decision,
)


@pytest.mark.parametrize(
    ("payload", "expected_action", "expected_error"),
    [
        (
            {
                "review_decision_code": 0,
                "review_status": "approved",
                "environment_check": {"status": "ready"},
            },
            WorkflowRouteAction.CONTINUE,
            WorkflowErrorType.NONE,
        ),
        (
            {
                "review_decision_code": 0,
                "review_status": "approved",
                "environment_check": {"status": "changes_required", "blocking_reason": "venv needs repair"},
            },
            WorkflowRouteAction.TESTER_ENVIRONMENT_REPAIR,
            WorkflowErrorType.ENVIRONMENT_ISSUE,
        ),
        (
            {
                "review_decision_code": 1,
                "review_status": "changes_required",
                "environment_check": {"status": "ready"},
            },
            WorkflowRouteAction.EXECUTOR_FIX,
            WorkflowErrorType.SOURCE_BUG,
        ),
        (
            {
                "review_decision_code": 0,
                "review_status": "approved",
                "environment_check": {"status": "blocked", "blocking_reason": "unsupported platform"},
            },
            WorkflowRouteAction.BLOCK_TASK,
            WorkflowErrorType.ENVIRONMENT_BLOCKED,
        ),
        (
            {
                "review_decision_code": -1,
                "review_status": "blocked",
                "environment_check": {"status": "ready"},
            },
            WorkflowRouteAction.BLOCK_TASK,
            WorkflowErrorType.REVIEW_BLOCKED,
        ),
    ],
)
def test_review_route_matrix(payload, expected_action, expected_error) -> None:
    route = route_review_payload(payload)

    assert route.action == expected_action
    assert route.error_type == expected_error


@pytest.mark.parametrize(
    ("decision", "expected_action", "expected_error"),
    [
        (
            HarnessTesterResult("tests_passed", "continue", "none", "ok", Path("tester_result.json"), {}, False, ()),
            WorkflowRouteAction.CONTINUE,
            WorkflowErrorType.NONE,
        ),
        (
            HarnessTesterResult("source_bug", "fix_code", "source_bug", "broken", Path("tester_result.json"), {}, False, ()),
            WorkflowRouteAction.EXECUTOR_FIX,
            WorkflowErrorType.SOURCE_BUG,
        ),
        (
            HarnessTesterResult("source_bug", "fix_code", "env_setup", "missing dep", Path("tester_result.json"), {}, True, ()),
            WorkflowRouteAction.TESTER_ENVIRONMENT_REPAIR,
            WorkflowErrorType.ENVIRONMENT_ISSUE,
        ),
        (
            HarnessTesterResult("environment_blocked", "block_task", "env_setup", "blocked", Path("tester_result.json"), {}, True, ()),
            WorkflowRouteAction.TESTER_ENVIRONMENT_REPAIR,
            WorkflowErrorType.ENVIRONMENT_ISSUE,
        ),
    ],
)
def test_tester_route_matrix(decision, expected_action, expected_error) -> None:
    route = route_tester_decision(decision)

    assert route.action == expected_action
    assert route.error_type == expected_error
