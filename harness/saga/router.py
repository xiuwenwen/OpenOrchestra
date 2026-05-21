from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.domain import RouteAction
from harness.events import EventEnvelope, TraceContext
from harness.saga.model import SagaDefinition


class SagaRoutingError(ValueError):
    pass


@dataclass(frozen=True)
class SagaRouteDecision:
    saga_name: str
    step_name: str
    event_type: str
    target_step: str
    action: RouteAction

    def to_payload(self) -> dict[str, Any]:
        return {
            "saga_name": self.saga_name,
            "step_name": self.step_name,
            "event_type": self.event_type,
            "target_step": self.target_step,
            "route_action": self.action.value,
        }


class SagaRouter:
    def __init__(self, definition: SagaDefinition) -> None:
        self.definition = definition

    def route_event(self, step_name: str, event_type: str) -> SagaRouteDecision:
        step = self.definition.step(step_name)
        for route in step.routes:
            if route.on_event == event_type:
                return SagaRouteDecision(
                    saga_name=self.definition.name,
                    step_name=step_name,
                    event_type=event_type,
                    target_step=route.target_step,
                    action=route.action,
                )
        raise SagaRoutingError(f"no route for {self.definition.name}.{step_name} on {event_type}")

    def route_event_envelope(
        self,
        *,
        task_id: str,
        step_name: str,
        event_type: str,
        trace: TraceContext,
        payload: dict[str, Any] | None = None,
    ) -> tuple[SagaRouteDecision, EventEnvelope]:
        decision = self.route_event(step_name, event_type)
        route_payload = decision.to_payload()
        if payload:
            route_payload.update(payload)
        event = EventEnvelope.create(
            "SagaRouteDecided",
            task_id=task_id,
            trace=trace,
            aggregate_type="workflow",
            aggregate_id=task_id,
            payload=route_payload,
        )
        return decision, event
