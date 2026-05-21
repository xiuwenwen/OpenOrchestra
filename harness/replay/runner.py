from __future__ import annotations

from dataclasses import dataclass

from harness.events import EventStore
from harness.replay.fixtures import ReplayFixture
from harness.saga import SagaRouteDecision, SagaRouter


@dataclass(frozen=True)
class ReplayRouteResult:
    expected_step: str
    expected_event: str
    actual: SagaRouteDecision

    @property
    def ok(self) -> bool:
        return (
            self.actual.step_name == self.expected_step
            and self.actual.event_type == self.expected_event
        )


class ReplayRunner:
    def __init__(self, *, event_store: EventStore, saga_router: SagaRouter) -> None:
        self.event_store = event_store
        self.saga_router = saga_router

    def load(self, fixture: ReplayFixture) -> tuple[ReplayRouteResult, ...]:
        self.event_store.append_many(fixture.events)
        results: list[ReplayRouteResult] = []
        for expectation in fixture.route_expectations:
            decision = self.saga_router.route_event(expectation.step_name, expectation.event_type)
            if decision.target_step != expectation.target_step:
                raise AssertionError(
                    f"{fixture.name} expected target {expectation.target_step!r}, got {decision.target_step!r}"
                )
            if decision.action.value != expectation.route_action:
                raise AssertionError(
                    f"{fixture.name} expected action {expectation.route_action!r}, got {decision.action.value!r}"
                )
            results.append(
                ReplayRouteResult(
                    expected_step=expectation.step_name,
                    expected_event=expectation.event_type,
                    actual=decision,
                )
            )
        return tuple(results)
