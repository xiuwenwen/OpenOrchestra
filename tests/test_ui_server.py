from __future__ import annotations

from pathlib import Path

import pytest

from harness.agents.result import ArtifactRef
from harness.artifacts.hashing import sha256_file
from harness.core.progress import ProgressEvent
from harness.core.state_machine import PLANNING_DRAFT
from harness.state.db import StateDB
from harness.state.repository import StateRepository
from harness.ui.server import DisplayTranslator, HarnessStateView, UiEventStore, _html


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
        }
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

    assert "data-file-path=" in html
    assert "openFile('" not in html


def test_ui_role_card_labels_artifacts_by_agent() -> None:
    html = _html()

    assert "latestRolePhaseRuns" in html
    assert "${item.agent_id} -> ${item.artifact.artifact_type}" in html


def test_ui_html_auto_follows_running_latest_task() -> None:
    html = _html()

    assert 'latestTask.status === "RUNNING"' in html
    assert "currentTask !== taskList.latest_task_id" in html


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
