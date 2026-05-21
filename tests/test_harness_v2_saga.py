from __future__ import annotations

import pytest

from harness.domain import RouteAction
from harness.saga import RetryPolicy, SagaDefinition, SagaRoute, SagaStep, build_bugfix_v2_saga


def test_bugfix_v2_saga_contains_contract_changed_retest_route() -> None:
    saga = build_bugfix_v2_saga()

    tester_routes = saga.step("tester_verify").routes

    assert any(
        route.on_event == "ContractChanged"
        and route.target_step == "tester_verify"
        and route.action == RouteAction.RETEST_CURRENT_REPO_SNAPSHOT
        for route in tester_routes
    )


def test_saga_definition_rejects_unknown_route_targets() -> None:
    step = SagaStep(
        name="one",
        command_type="DoWork",
        expected_events=("Done",),
        timeout_seconds=1,
        retry_policy=RetryPolicy(max_attempts=1),
        routes=(SagaRoute("Done", "missing", RouteAction.CONTINUE),),
    )

    with pytest.raises(ValueError, match="route target not found"):
        SagaDefinition(name="bad", initial_step="one", steps=(step,))


def test_saga_retry_policy_requires_positive_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts must be positive"):
        RetryPolicy(max_attempts=0)
