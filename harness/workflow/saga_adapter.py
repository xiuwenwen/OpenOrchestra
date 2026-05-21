from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.core.progress import ProgressEvent
from harness.core.taxonomy import CONTRACT_BUG, CONTRACT_INVALID
from harness.events import EventEnvelope, TraceContext
from harness.saga import SagaRouteDecision, SagaRouter, build_bugfix_v2_saga
from harness.testing.tester_result import TesterResult


class BugfixSagaAdapter:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.router = SagaRouter(build_bugfix_v2_saga())

    def emit_started(self, task_id: str) -> None:
        event = EventEnvelope.create(
            "SagaStarted",
            task_id=task_id,
            trace=self.trace(task_id),
            aggregate_type="workflow",
            aggregate_id=task_id,
            payload={"saga_name": self.router.definition.name},
        )
        self.append_event(event)

    def record_route(
        self,
        task_id: str,
        step_name: str,
        event_type: str,
        *,
        phase: str,
        round_id: int,
        payload: dict[str, Any] | None = None,
    ) -> SagaRouteDecision:
        decision, event = self.router.route_event_envelope(
            task_id=task_id,
            step_name=step_name,
            event_type=event_type,
            trace=self.trace(task_id).child(f"{step_name}:{event_type}:{round_id}"),
            payload=payload,
        )
        self.append_event(event)
        self.runtime.emit_progress(
            ProgressEvent(
                "saga_route_decision",
                task_id=task_id,
                phase=phase,
                role="orchestrator",
                round_id=round_id,
                status=decision.action.value,
                message=f"{decision.saga_name}.{decision.step_name} routed {event_type} to {decision.target_step}",
                data=decision.to_payload(),
            )
        )
        return decision

    def append_event(self, event: EventEnvelope) -> None:
        event_store = getattr(self.runtime, "event_store", None)
        if event_store is not None:
            event_store.append(event)

    def trace(self, task_id: str) -> TraceContext:
        return TraceContext.start(trace_id=f"{task_id}:bugfix-v2", correlation_id=task_id)

    def materialized_event_type(self, task_id: str, round_id: int) -> str:
        changed_files = self.latest_merged_patch_changed_files(task_id, round_id)
        if changed_files == []:
            return "SnapshotUnchangedButRetestable"
        return "SnapshotChanged"

    def latest_merged_patch_changed_files(self, task_id: str, round_id: int) -> list[str] | None:
        for artifact in reversed(self.runtime.repository.list_artifacts(task_id, "merged_patch_metadata.json")):
            artifact_round_id = artifact.get("round_id")
            if artifact_round_id is not None:
                try:
                    if int(artifact_round_id) != round_id:
                        continue
                except (TypeError, ValueError):
                    pass
            path = Path(artifact["path"])
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                return None
            changed_files = payload.get("changed_files") if isinstance(payload, dict) else None
            if isinstance(changed_files, list) and all(isinstance(item, str) for item in changed_files):
                return changed_files
            return None
        return None

    def tester_event_type(self, decision: TesterResult) -> str:
        if decision.failure_type in {CONTRACT_BUG, CONTRACT_INVALID}:
            return "ContractChanged"
        if decision.tests_passed:
            return "GatePassed"
        if decision.source_bug:
            return "SourceBugDetected"
        if decision.environment_blocked or decision.has_environment_dependency_issue:
            return "EnvironmentBlocked"
        return "DecisionRejected"
