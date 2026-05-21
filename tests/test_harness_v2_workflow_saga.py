from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from harness.events import SQLiteEventStore
from harness.testing.tester_result import TESTS_PASSED, TesterResult as HarnessTesterResult
from harness.workflow.engine import WorkflowEngine


class _Repository:
    def __init__(self) -> None:
        self.artifacts: dict[str, list[dict[str, object]]] = {"merged_patch_metadata.json": []}

    def get_task(self, task_id: str) -> dict[str, object]:
        return {"task_id": task_id, "prompt_turn_id": 0, "configuration": {}}

    def list_phases(self, task_id: str) -> list[dict[str, object]]:
        return []

    def list_artifacts(self, task_id: str, artifact_type: str) -> list[dict[str, object]]:
        return list(self.artifacts.get(artifact_type, []))


class _Runtime:
    def __init__(self, tmp_path: Path) -> None:
        self.config = {"limits": {"max_test_fix_rounds": 1, "max_review_rounds": 1}}
        self.repository = _Repository()
        self.event_store = SQLiteEventStore(tmp_path / "events.sqlite3")
        self.logger = SimpleNamespace(warning=lambda message: None)
        self.fix_round_limit_callback = None
        self.progress_events = []

    def is_failed_resume(self, task_id: str) -> bool:
        return False

    def emit_progress(self, event) -> None:
        self.progress_events.append(event)

    def run_role_phase(self, *args, **kwargs):
        return []

    def run_patch_merge(self, task_id: str, round_id: int, user_prompt: str) -> bool:
        return True


def test_workflow_engine_records_bugfix_saga_routes_on_main_chain(tmp_path: Path, monkeypatch) -> None:
    runtime = _Runtime(tmp_path)
    engine = WorkflowEngine(runtime)
    tester_result = HarnessTesterResult(
        status=TESTS_PASSED,
        next_action="continue",
        failure_type="none",
        summary="tests passed",
        artifact_path=tmp_path / "tester_result.json",
        payload={},
        environment_dependency_issue=False,
    )
    monkeypatch.setattr(engine, "run_initial_planning_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "run_testing_until_tester_decision", lambda *args, **kwargs: tester_result)
    monkeypatch.setattr(engine, "run_review_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "run_final_validation_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "run_delivery", lambda *args, **kwargs: tmp_path / "delivery")

    delivery = engine.run_bugfix_flow("task-saga", "fix the bug")

    assert delivery == tmp_path / "delivery"
    route_events = [
        event.payload
        for event in runtime.event_store.replay("task-saga")
        if event.event_type == "SagaRouteDecided"
    ]
    assert [(event["step_name"], event["event_type"], event["target_step"]) for event in route_events] == [
        ("plan", "DecisionAccepted", "execute_patch"),
        ("execute_patch", "ArtifactCanonicalized", "materialize"),
        ("materialize", "SnapshotChanged", "tester_verify"),
        ("tester_verify", "GatePassed", "review"),
    ]
    assert [event.event_type for event in runtime.event_store.replay("task-saga")][0] == "SagaStarted"


def test_workflow_engine_routes_empty_changed_files_as_retest(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    metadata = tmp_path / "merged_patch_metadata.json"
    metadata.write_text('{"changed_files": []}', encoding="utf-8")
    runtime.repository.artifacts["merged_patch_metadata.json"] = [
        {"path": str(metadata), "round_id": 1},
    ]
    engine = WorkflowEngine(runtime)

    decision = engine.bugfix_saga.record_route(
        "task-saga",
        "materialize",
        engine.bugfix_saga.materialized_event_type("task-saga", 1),
        phase="PATCH_MERGE",
        round_id=1,
    )

    assert decision.target_step == "tester_verify"
    assert decision.action.value == "retest_current_repo_snapshot"
