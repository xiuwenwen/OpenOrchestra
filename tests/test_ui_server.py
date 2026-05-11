from __future__ import annotations

from pathlib import Path

import pytest

from harness.agents.result import ArtifactRef
from harness.artifacts.hashing import sha256_file
from harness.config.loader import load_config
from harness.core.progress import ProgressEvent
from harness.core.state_machine import FIXING, PATCH_MERGE, PLANNING_DRAFT, TESTING
from harness.state.db import StateDB
from harness.state.repository import StateRepository
from harness.ui.api import (
    api_error_payload,
    parse_json_object_body,
    require_string_field,
    validate_runtime_config_payload,
)
from harness.ui.server import _html
from harness.ui.state_view import HarnessStateView, UiEventStore
from harness.ui.translation import DisplayTranslator


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
        },
        "agent_backend": {
            "default": "codex",
            "planner": "codex",
            "executor": "codex",
            "tester": "codex",
            "reviewer": "codex",
            "judge": "codex",
            "communicator": "codex",
        },
        "roles": {
            "planner": {"count": 2},
            "executor": {"count": 2},
            "tester": {"count": 1},
            "reviewer": {"count": 1},
            "judge": {"count": 1},
            "communicator": {"count": 1},
        },
    }


def test_ui_snapshot_includes_agent_logs_and_artifacts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    store = UiEventStore()
    task_id = repo.create_task("build a viewer", workflow_type="feature_change")
    phase_id = repo.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    run_id = repo.create_agent_run(task_id, phase_id, "planner", "planner-1", 0)
    repo.update_agent_run_status(run_id, "COMPLETED")
    log_dir = (
        Path(config["system"]["workspace_root"])
        / task_id
        / phase_id
        / "planner"
        / "planner-1"
        / "round_0"
        / "attempt_0"
        / "logs"
    )
    log_dir.mkdir(parents=True)
    (log_dir / "prompt.md").write_text("prompt", encoding="utf-8")
    (log_dir / "stdout.log").write_text("visible output", encoding="utf-8")
    artifact_path = tmp_path / "plan.md"
    artifact_path.write_text("plan", encoding="utf-8")
    repo.create_artifact(
        ArtifactRef(
            artifact_id="artifact-1",
            task_id=task_id,
            phase_id=phase_id,
            role="planner",
            agent_id="planner-1",
            artifact_type="plan.md",
            path=artifact_path,
            version=1,
            hash="hash",
        )
    )
    store(ProgressEvent("agent_completed", task_id=task_id, role="planner", agent_id="planner-1", status="COMPLETED"))

    snapshot = HarnessStateView(config, repo, store).snapshot(task_id)

    assert snapshot["task"]["task_id"] == task_id
    assert snapshot["agent_runs"][0]["prompt_path"]["exists"] is True
    assert snapshot["agent_runs"][0]["stdout_path"]["exists"] is True
    assert snapshot["agent_runs"][0]["artifact_count"] == 1
    assert snapshot["role_rounds"]["planner"][0]["round_id"] == 0
    assert snapshot["role_rounds"]["planner"][0]["runs"][0]["agent_id"] == "planner-1"
    assert snapshot["events"][0]["event_type"] == "agent_completed"


def test_ui_event_store_preserves_trace_fields() -> None:
    store = UiEventStore()
    store(
        ProgressEvent(
            "agent_started",
            task_id="task-1",
            role="executor",
            agent_id="executor-1",
            trace_id="trace-1",
            span_id="span-1",
            parent_span_id="parent-1",
        )
    )

    event = store.events_for("task-1")[0]
    assert event["trace_id"] == "trace-1"
    assert event["span_id"] == "span-1"
    assert event["parent_span_id"] == "parent-1"


def test_ui_snapshot_preserves_workflow_loops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    store = UiEventStore()
    task_id = repo.create_task("fix loop", workflow_type="bugfix")
    repo.create_phase(task_id, PATCH_MERGE, "executor", 0)
    repo.create_phase(task_id, TESTING, "tester", 0)
    repo.create_phase(task_id, FIXING, "executor", 1)
    repo.create_phase(task_id, PATCH_MERGE, "executor", 1)
    repo.create_phase(task_id, TESTING, "tester", 1)

    snapshot = HarnessStateView(config, repo, store).snapshot(task_id)

    timeline = snapshot["workflow_timeline"]
    assert [item["phase_type"] for item in timeline] == [PATCH_MERGE, TESTING, FIXING, PATCH_MERGE, TESTING]
    assert timeline[3]["loop_revisit"] is True
    assert timeline[4]["loop_revisit"] is True
    assert snapshot["workflow_loop_edges"] == [
        {"phase_type": PATCH_MERGE, "from_index": 0, "to_index": 3, "from_round": 0, "to_round": 1},
        {"phase_type": TESTING, "from_index": 1, "to_index": 4, "from_round": 0, "to_round": 1},
    ]


def test_ui_snapshot_exposes_backend_health_events(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    store = UiEventStore()
    task_id = repo.create_task("backend health", workflow_type="bugfix")
    store(
        ProgressEvent(
            "backend_health_changed",
            task_id=task_id,
            status="OPEN",
            message="backend claude circuit opened",
            data={
                "backend": "claude",
                "backend_health_state": "open",
                "backend_health_allowed": False,
                "backend_consecutive_failures": 3,
                "backend_failure_kind": "timeout",
            },
        )
    )

    snapshot = HarnessStateView(config, repo, store).snapshot(task_id)

    assert snapshot["backend_health"]["claude"]["state"] == "open"
    assert snapshot["backend_health"]["claude"]["allowed"] is False
    assert snapshot["backend_health"]["claude"]["failure_kind"] == "timeout"


def test_ui_file_reader_is_limited_to_harness_roots(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    store = UiEventStore()
    view = HarnessStateView(config, repo, store)
    allowed = Path(config["system"]["workspace_root"]) / "x.log"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("ok", encoding="utf-8")

    assert view.read_file(str(allowed))["text"] == "ok"
    with pytest.raises(PermissionError):
        view.read_file(str(tmp_path / "outside.txt"))


def test_ui_store_can_select_resumed_task() -> None:
    store = UiEventStore()

    store.select_task("task-123")

    assert store.latest_task_id == "task-123"


def test_api_error_payload_has_stable_code_and_message() -> None:
    assert api_error_payload("bad_request", "invalid") == {
        "error": {"code": "bad_request", "message": "invalid"}
    }


def test_ui_api_json_body_must_be_an_object() -> None:
    assert parse_json_object_body(b'{"text":"ok"}', "/api/translate") == {"text": "ok"}
    with pytest.raises(ValueError, match="valid JSON"):
        parse_json_object_body(b"{", "/api/translate")
    with pytest.raises(ValueError, match="JSON object"):
        parse_json_object_body(b'["text"]', "/api/translate")


def test_ui_api_translate_text_must_be_a_bounded_string() -> None:
    assert require_string_field({"text": "hello"}, "text", "/api/translate", 10) == "hello"
    with pytest.raises(ValueError, match="must be a string"):
        require_string_field({"text": 1}, "text", "/api/translate", 10)
    with pytest.raises(ValueError, match="exceeds 3 characters"):
        require_string_field({"text": "long"}, "text", "/api/translate", 3)


def test_ui_api_runtime_config_rejects_unknown_or_invalid_fields() -> None:
    payload = {"agent_backend": {}, "roles": {}, "persist": False}
    assert validate_runtime_config_payload(payload) == payload
    with pytest.raises(ValueError, match="unsupported field"):
        validate_runtime_config_payload({"unknown": True})
    with pytest.raises(ValueError, match="persist must be a boolean"):
        validate_runtime_config_payload({"persist": "yes"})


def test_ui_runtime_config_updates_shared_config(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    view = HarnessStateView(config, repo, UiEventStore())

    updated = view.update_runtime_config(
        {
            "agent_backend": {"planner": "claude", "executor": "codex"},
            "roles": {"planner": {"count": 3}, "executor": {"count": 1}},
        }
    )

    assert config["agent_backend"]["planner"] == "claude"
    assert config["roles"]["planner"]["count"] == 3
    assert updated["agent_backend"]["planner"] == "claude"
    assert updated["roles"]["planner"]["count"] == 3
    assert updated["persist_supported"] is False


def test_ui_runtime_config_can_persist_to_config_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "config.yaml"
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    view = HarnessStateView(config, repo, UiEventStore(), config_path=config_path)

    updated = view.update_runtime_config(
        {
            "agent_backend": {"planner": "claude"},
            "roles": {"planner": {"count": 4}},
            "persist": True,
        }
    )

    persisted = load_config(config_path)
    assert updated["persist_supported"] is True
    assert persisted["agent_backend"]["planner"] == "claude"
    assert persisted["roles"]["planner"]["count"] == 4


def test_ui_runtime_config_rejects_unknown_backend(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    view = HarnessStateView(config, repo, UiEventStore())

    with pytest.raises(ValueError, match="Unsupported backend"):
        view.update_runtime_config({"agent_backend": {"planner": "unknown"}, "roles": {}})


def test_ui_runtime_config_locks_while_task_is_active(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    view = HarnessStateView(config, repo, UiEventStore())
    task_id = repo.create_task("running task")
    repo.update_task(task_id, status="FIXING", current_phase="FIXING", current_role="executor")

    assert view.has_active_task() is True


def test_ui_store_tracks_incremental_events_for_live_dashboard() -> None:
    store = UiEventStore()
    store(ProgressEvent("task_started", task_id="task-1", status="RUNNING"))
    store(ProgressEvent("agent_started", task_id="task-1", role="executor", agent_id="executor-1", status="RUNNING"))
    store(ProgressEvent("task_started", task_id="task-2", status="RUNNING"))

    first_task_events = store.events_since(0, "task-1")
    all_events_after_first = store.events_since(first_task_events[0]["id"])

    assert [event["event_type"] for event in first_task_events] == ["task_started", "agent_started"]
    assert first_task_events[0]["id"] < first_task_events[1]["id"]
    assert [event["task_id"] for event in all_events_after_first] == ["task-1", "task-2"]
    assert store.latest_event_id() == 3


def test_ui_snapshot_uses_recorded_success_path_artifact(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    store = UiEventStore()
    task_id = repo.create_task("prompt whose slug is not the published directory", workflow_type="new_project")
    success_path = tmp_path / "deliver" / "actual-published-location" / "success_path.md"
    success_path.parent.mkdir(parents=True)
    success_path.write_text("success_path: actual\n", encoding="utf-8")
    repo.create_artifact(
        ArtifactRef(
            artifact_id="success-artifact",
            task_id=task_id,
            phase_id=None,
            role="orchestrator",
            agent_id="harness",
            artifact_type="success_path.md",
            path=success_path,
            version=1,
            hash=sha256_file(success_path),
        )
    )

    snapshot = HarnessStateView(config, repo, store).snapshot(task_id)

    assert snapshot["success_path"] == str(success_path.parent)


def test_ui_html_uses_data_attributes_for_dynamic_file_buttons() -> None:
    html = _html()

    assert "openFile(" in html
    assert "fbtn" in html


def test_ui_html_includes_live_event_stream() -> None:
    html = _html()

    assert 'id="logBody"' in html
    assert "new EventSource" in html
    assert "/api/events?task=" in html


def test_ui_html_auto_refreshes_open_live_logs() -> None:
    html = _html()

    assert "refreshCurrentFile" in html
    assert "scheduleFileRefresh" in html
    assert "isLiveLogLabel" in html
    assert "实时日志" in html


def test_ui_html_renders_workflow_loop_markers() -> None:
    html = _html()

    assert "workflow_loop_edges" in html
    assert "loop-tag" in html
    assert "loop_revisit" in html


def test_ui_html_hides_low_value_run_summary_sections() -> None:
    html = _html()

    assert 'id="runs"' not in html
    assert "renderRuns" not in html
    assert "All Runs" not in html
    assert "全部执行记录" not in html
    assert "Failures / Retries" not in html
    assert "失败/重试" not in html
    assert "completedArtifacts" not in html


def test_ui_role_card_labels_artifacts_by_agent() -> None:
    html = _html()

    assert "renderAgentCards" in html
    assert "ag-card" in html


def test_ui_labels_output_invalid_as_contract_issue_not_test_failure() -> None:
    html = _html()

    assert "产物格式无效" in html
    assert "Output Contract Invalid" in html
    assert "不代表测试结论失败" in html
    assert "This is not the test verdict" in html
    assert "业务测试失败" in html
    assert "patch gate失败" in html
    assert "Agent执行失败" in html
    assert "Business tests failed" in html
    assert "Patch gate failed" in html
    assert "Agent execution failed" in html
    assert ".pill.OUTPUT_INVALID{background:var(--warn-soft)" in html


def test_ui_html_auto_follows_running_latest_task() -> None:
    html = _html()

    assert 'lt.status==="RUNNING"' in html
    assert "currentTask!==tl.latest_task_id" in html


def test_display_translator_fallback_translates_prompt_prose_and_keeps_paths(monkeypatch, tmp_path: Path) -> None:
    translator = DisplayTranslator(_config(tmp_path))
    monkeypatch.setattr(translator, "_translation_backend", lambda: None)

    result = translator.translate_to_zh(
        "\n".join(
            [
                "## User Request",
                "Workflow classification: new_project.",
                "Use the full new-project workflow from planning through final delivery.",
                "Original user prompt:",
                "/Users/example/project/plan.md",
            ]
        )
    )

    assert result["mode"] == "fallback"
    assert "## 用户请求" in result["text"]
    assert "工作流分类：新项目。" in result["text"]
    assert "使用完整的新项目工作流" in result["text"]
    assert "/Users/example/project/plan.md" in result["text"]
