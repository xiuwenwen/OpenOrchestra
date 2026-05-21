from __future__ import annotations

from pathlib import Path

from harness.events import SQLiteEventStore
from harness.replay import ReplayRunner, load_replay_fixture
from harness.saga import SagaRouter, build_bugfix_v2_saga


def test_replay_fixture_routes_noop_patch_to_retest(tmp_path: Path) -> None:
    fixture = load_replay_fixture(Path("tests/fixtures/replay/no_op_patch_stream.json"))
    runner = ReplayRunner(
        event_store=SQLiteEventStore(tmp_path / "events.sqlite3"),
        saga_router=SagaRouter(build_bugfix_v2_saga()),
    )

    results = runner.load(fixture)

    assert len(results) == 1
    assert results[0].actual.target_step == "tester_verify"
    assert results[0].actual.action.value == "retest_current_repo_snapshot"


def test_replay_fixture_covers_tester_contract_and_source_routes(tmp_path: Path) -> None:
    fixture = load_replay_fixture(Path("tests/fixtures/replay/tester_route_stream.json"))
    event_store = SQLiteEventStore(tmp_path / "events.sqlite3")
    runner = ReplayRunner(
        event_store=event_store,
        saga_router=SagaRouter(build_bugfix_v2_saga()),
    )

    results = runner.load(fixture)

    assert [result.actual.event_type for result in results] == ["ContractChanged", "SourceBugDetected"]
    assert [result.actual.target_step for result in results] == ["tester_verify", "execute_patch"]
    assert [event.event_type for event in event_store.replay("replay-tester-route")] == [
        "ContractChanged",
        "SourceBugDetected",
    ]
