from __future__ import annotations

import io
import sys
from pathlib import Path

import harness.cli.interactive as interactive_module
import harness.main as main_module
from harness.delivery import handoff as handoff_module
from harness.agents.result import ArtifactRef
from harness.core.progress import ProgressEvent
from harness.core.workflow_classifier import WorkflowClassification
from harness.main import ConsoleProgressReporter, InteractiveCLI
from harness.state.db import StateDB
from harness.state.repository import StateRepository


class _TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
        },
        "agent_backend": {
            "default": "mock",
            "planner": "mock",
            "executor": "mock",
            "tester": "mock",
            "reviewer": "mock",
            "judge": "mock",
            "communicator": "mock",
        },
        "roles": {
            "planner": {"count": 1},
            "executor": {"count": 1},
            "tester": {"count": 1},
            "reviewer": {"count": 1},
            "judge": {"count": 1},
            "communicator": {"count": 1},
        },
        "limits": {
            "max_planning_rounds": 3,
            "max_test_fix_rounds": 10,
            "max_review_rounds": 3,
            "max_agent_retry": 2,
        },
        "timeouts": {
            "planner": 5,
            "executor": 5,
            "tester": 5,
            "reviewer": 5,
            "judge": 5,
            "communicator": 5,
        },
        "policy": {
            "different_roles_can_run_concurrently": False,
            "same_role_can_run_concurrently": True,
            "allow_medium_bug_delivery": False,
            "require_all_tests_pass": True,
        },
        "heartbeat": {"interval_seconds": 60},
        "visualization": {"host": "127.0.0.1", "port": 8765},
        "claude": {
            "context_window_tokens": 199999,
            "context_window_buffer_tokens": 2048,
            "max_output_tokens": {
                "classifier": 2048,
                "misc": 64000,
                "planner": 64000,
                "executor": 64000,
                "tester": 64000,
                "reviewer": 64000,
                "judge": 64000,
                "communicator": 64000,
            }
        },
        "artifact_input": {"max_files": 50, "max_file_bytes": 262144, "max_total_bytes": 1048576},
    }


def _classification(workflow_type: str, score: int = 3) -> WorkflowClassification:
    return WorkflowClassification(
        workflow_type=workflow_type,
        confidence=0.8,
        difficulty_score=score,
        difficulty_reason="test difficulty",
        reason="test reason",
    )


def test_resume_context_does_not_pollute_project_workflow_classification(monkeypatch, tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    historical_task_id = cli.orchestrator.create_task("Build a small weather app")
    cli.active_task_id = historical_task_id
    captured: dict[str, str] = {}

    def fake_classify(prompt: str, backend: str, config: dict | None = None) -> tuple[WorkflowClassification, None]:
        captured["classified_prompt"] = prompt
        captured["backend"] = backend
        captured["config_seen"] = "yes" if config else "no"
        return _classification("feature_change"), None

    def fake_run_existing(
        orchestrator,
        task_id: str,
        prompt: str,
        workflow_type: str,
        project_context_md: str | None = None,
        classification: WorkflowClassification | None = None,
    ) -> int:
        captured["task_id"] = task_id
        captured["run_prompt"] = prompt
        captured["workflow_type"] = workflow_type
        captured["project_context_md"] = project_context_md or ""
        captured["classification"] = classification.workflow_type if classification else ""
        return 0

    monkeypatch.setattr(interactive_module, "classify_workflow_with_metadata", fake_classify)
    monkeypatch.setattr(interactive_module, "run_existing", fake_run_existing)
    monkeypatch.setattr(interactive_module, "run_once", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("active project follow-up must not create a new task")))

    cli._run_prompt("add an export button to this project")

    assert captured["classified_prompt"] == "add an export button to this project"
    assert captured["backend"] == "mock"
    assert captured["config_seen"] == "yes"
    assert captured["task_id"] == historical_task_id
    assert captured["workflow_type"] == "feature_change"
    assert captured["classification"] == "feature_change"
    assert captured["run_prompt"] == "add an export button to this project"
    assert "Historical task id:" in captured["project_context_md"]


def test_misc_prompt_uses_direct_chat_without_harness_task(monkeypatch, tmp_path: Path, capsys) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    historical_task_id = cli.orchestrator.create_task("Build a small weather app")
    cli.active_task_id = historical_task_id
    capsys.readouterr()
    captured: dict[str, str | None] = {}

    class FakeMiscChatRunner:
        def __init__(self, backend: str, config: dict | None = None):
            captured["backend"] = backend
            captured["config_seen"] = "yes" if config else "no"

        def ask(self, prompt: str, context: str | None = None) -> str:
            captured["prompt"] = prompt
            captured["context"] = context
            return "direct answer"

    def fail_run_once(*args, **kwargs) -> int:
        raise AssertionError("misc must not run Harness task flow")

    monkeypatch.setattr(interactive_module, "classify_workflow_with_metadata", lambda prompt, backend, config=None: (_classification("misc"), None))
    monkeypatch.setattr(interactive_module, "MiscChatRunner", FakeMiscChatRunner)
    monkeypatch.setattr(interactive_module, "run_once", fail_run_once)

    cli._run_prompt("how do I use this project?")

    output = capsys.readouterr().out
    assert output.strip() == "direct answer"
    assert "[classifier]" not in output
    assert captured["backend"] == "mock"
    assert captured["config_seen"] == "yes"
    assert captured["prompt"] == "how do I use this project?"
    assert captured["context"]
    assert "Historical task id:" in captured["context"]


def test_active_context_question_skips_classifier_and_new_task(monkeypatch, tmp_path: Path, capsys) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    historical_task_id = cli.orchestrator.create_task("Build a small weather app")
    cli.active_task_id = historical_task_id
    capsys.readouterr()
    captured: dict[str, str | None] = {}

    class FakeMiscChatRunner:
        def __init__(self, backend: str, config: dict | None = None):
            captured["backend"] = backend

        def ask(self, prompt: str, context: str | None = None) -> str:
            captured["prompt"] = prompt
            captured["context"] = context
            return "context answer"

    monkeypatch.setattr(interactive_module, "classify_workflow_with_metadata", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("classifier should not run for active-context question")))
    monkeypatch.setattr(interactive_module, "MiscChatRunner", FakeMiscChatRunner)
    monkeypatch.setattr(interactive_module, "run_once", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("question should not create task")))

    cli._run_prompt("这个项目怎么启动？")

    assert capsys.readouterr().out.strip() == "context answer"
    assert captured["prompt"] == "这个项目怎么启动？"
    assert "Historical task id:" in (captured["context"] or "")
    assert cli.active_task_id == historical_task_id


def test_misc_classifier_fallback_prints_raw_answer_only(monkeypatch, tmp_path: Path, capsys) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())

    class FailMiscChatRunner:
        def __init__(self, backend: str, config: dict | None = None):
            pass

        def ask(self, prompt: str, context: str | None = None) -> str:
            raise AssertionError("fallback answer should avoid a second model call")

    monkeypatch.setattr(interactive_module, "classify_workflow_with_metadata", lambda prompt, backend, config=None: (_classification("misc"), "raw answer"))
    monkeypatch.setattr(interactive_module, "MiscChatRunner", FailMiscChatRunner)

    cli._run_prompt("how do I use this project?")

    output = capsys.readouterr().out
    assert output.strip() == "raw answer"
    assert "[classifier]" not in output


def test_delivery_handoff_prefers_source_dir_and_requirements(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        handoff_module.shutil,
        "which",
        lambda command: f"/usr/bin/{command}" if command in {"python", "python3"} else None,
    )
    delivery_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = delivery_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (source_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
    final_delivery = delivery_dir / "final_delivery.json"
    final_delivery.write_text("# Final Delivery\n", encoding="utf-8")
    usage_guide = delivery_dir / "usage_guide.md"
    usage_guide.write_text("```bash\npython app.py\n```\n", encoding="utf-8")

    lines = main_module.format_delivery_handoff(final_delivery, usage_guide)

    assert lines[0] == f"project_dir: {source_dir}"
    assert lines[1] == f"run_command: cd {source_dir} && python3 app.py"
    assert lines[2].startswith(f"dependency_install: cd {source_dir} && PYTHON_BIN=")
    assert 'python3 || command -v python' in lines[2]
    assert '"$VENV_PYTHON" -m pip install -r requirements.txt' in lines[2]


def test_delivery_handoff_prefers_one_command_installer(tmp_path: Path) -> None:
    delivery_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = delivery_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (source_dir / "install_dependencies.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (source_dir / "tests").mkdir()
    final_delivery = delivery_dir / "final_delivery.json"
    final_delivery.write_text("# Final Delivery\n", encoding="utf-8")
    usage_guide = delivery_dir / "usage_guide.md"
    usage_guide.write_text("```bash\npython3 -m pytest tests/\n```\n", encoding="utf-8")

    lines = main_module.format_delivery_handoff(final_delivery, usage_guide)

    assert lines[1] == f"run_command: cd {source_dir} && .venv/bin/python -m pytest tests/"
    assert lines[2] == f"dependency_install: cd {source_dir} && bash install_dependencies.sh"


def test_delivery_handoff_skips_non_executable_doc_command_and_infers_python_entrypoint(tmp_path: Path) -> None:
    delivery_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = delivery_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    final_delivery = delivery_dir / "final_delivery.json"
    final_delivery.write_text("# Final Delivery\n", encoding="utf-8")
    usage_guide = delivery_dir / "usage_guide.md"
    usage_guide.write_text("```bash\npython missing.py\n```\n", encoding="utf-8")

    lines = main_module.format_delivery_handoff(final_delivery, usage_guide)

    assert lines[1] == f"run_command: cd {source_dir} && python3 main.py"


def test_delivery_handoff_infers_npm_script_when_available(monkeypatch, tmp_path: Path) -> None:
    delivery_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = delivery_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "package.json").write_text('{"scripts":{"start":"vite --host 127.0.0.1"}}', encoding="utf-8")
    final_delivery = delivery_dir / "final_delivery.json"
    final_delivery.write_text("# Final Delivery\n", encoding="utf-8")
    monkeypatch.setattr(handoff_module.shutil, "which", lambda command: f"/usr/bin/{command}" if command == "npm" else None)

    lines = main_module.format_delivery_handoff(final_delivery)

    assert lines[1] == f"run_command: cd {source_dir} && npm run start"


def test_delivery_handoff_uses_venv_python_for_script_commands_with_installer(tmp_path: Path) -> None:
    delivery_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = delivery_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "install_dependencies.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (source_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
    final_delivery = delivery_dir / "final_delivery.json"
    final_delivery.write_text("# Final Delivery\n", encoding="utf-8")
    usage_guide = delivery_dir / "usage_guide.md"
    usage_guide.write_text("```bash\npython3 app.py\n```\n", encoding="utf-8")

    lines = main_module.format_delivery_handoff(final_delivery, usage_guide)

    assert lines[1] == f"run_command: cd {source_dir} && .venv/bin/python app.py"


def test_format_total_elapsed_uses_task_timestamps() -> None:
    line = main_module.format_total_elapsed(
        {
            "created_at": "2026-05-07T10:00:00+00:00",
            "updated_at": "2026-05-07T11:02:03+00:00",
        }
    )

    assert line == "total_elapsed: 1h 2m 3s"


def test_dashboard_tty_task_completed_does_not_duplicate_handoff(monkeypatch, tmp_path: Path) -> None:
    delivery_dir = tmp_path / "deliver" / "project-12345678"
    delivery_dir.mkdir(parents=True)
    final_delivery = delivery_dir / "final_delivery.json"
    final_delivery.write_text("# Final Delivery\n", encoding="utf-8")
    output = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    reporter = main_module.DashboardProgressReporter()

    reporter(
        ProgressEvent(
            "task_completed",
            task_id="task-1",
            phase="COMPLETED",
            status="COMPLETED",
            data={"result_path": str(final_delivery), "result_type": "final_delivery"},
        )
    )

    rendered = output.getvalue()
    assert "\x1b[2J" not in rendered
    assert "OpenOrchestra Execution Dashboard" in rendered
    assert "project_dir:" not in rendered
    assert "[ok] COMPLETED COMPLETED" in rendered


def test_dashboard_tty_prints_events_above_bottom_panel(monkeypatch) -> None:
    output = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    reporter = main_module.DashboardProgressReporter()

    reporter(ProgressEvent("task_created", task_id="task-1", status="CREATED"))
    reporter(ProgressEvent("task_started", task_id="task-1", status="RUNNING"))

    rendered = output.getvalue()
    assert "\x1b[2J" not in rendered
    assert "\x1b[" in rendered and "F" in rendered
    assert "OpenOrchestra Execution Dashboard" in rendered
    assert "Recent events:" not in rendered
    assert rendered.rfind("[task] RUNNING") < rendered.rfind("OpenOrchestra Execution Dashboard")


def test_run_once_prints_user_handoff_not_internal_delivery_paths(tmp_path: Path, capsys) -> None:
    orchestrator = main_module.Orchestrator(_config(tmp_path))

    main_module.run_once(orchestrator, "Build a weather app", workflow_type="new_project")

    output = capsys.readouterr().out
    assert "project_dir:" in output
    assert "run_command:" in output
    assert "dependency_install:" in output
    assert "total_elapsed:" in output
    assert "final_delivery:" not in output
    assert "success_path:" not in output
    assert "usage_guide:" not in output


def test_history_context_includes_concrete_paths_for_misc_answers(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    task_id = cli.orchestrator.create_task("Fix the chess game", workflow_type="bugfix")
    phase_id = cli.orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0, status="COMPLETED")
    cli.orchestrator.repository.create_agent_run(
        task_id,
        phase_id,
        "executor",
        "executor-1",
        0,
        status="COMPLETED",
    )
    repo_path = (
        Path(cli.config["system"]["workspace_root"])
        / task_id
        / phase_id
        / "executor"
        / "executor-1"
        / "round_0"
        / "attempt_0"
        / "repo"
    )
    repo_path.mkdir(parents=True)
    merged_patch = tmp_path / "merged_patch.diff"
    merged_patch.write_text("diff --git a/app.js b/app.js\n", encoding="utf-8")
    success_dir = tmp_path / "deliver" / "project-12345678"
    success_dir.mkdir(parents=True)
    source_dir = success_dir / "source"
    source_dir.mkdir()
    (source_dir / "package.json").write_text('{"scripts":{"dev":"vite"}}\n', encoding="utf-8")
    success_path_md = success_dir / "success_path.md"
    success_path_md.write_text(f"success_path: {success_dir}\n", encoding="utf-8")
    for artifact_type, path in (("merged_patch.diff", merged_patch), ("success_path.md", success_path_md)):
        cli.orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=f"{task_id}-{artifact_type}",
                task_id=task_id,
                phase_id=phase_id,
                role="executor",
                agent_id="executor-1",
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash=None,
            )
        )

    context = cli._build_history_context(task_id)

    assert context
    assert f"- task_workspace: {Path(cli.config['system']['workspace_root']) / task_id}" in context
    assert f"- latest_agent_repo_workspace: {repo_path}" in context
    assert f"- success_path: {success_dir}" in context
    assert f"- materialized_source_candidate: {source_dir}" in context
    assert "- source_note: reconstructed from patch; validate completeness with final.patch and project tests." in context
    assert f"- merged_patch.diff: {merged_patch}" in context
    assert "Do not replace known concrete paths with placeholders" in context


def test_history_context_marks_incomplete_source_as_partial(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    task_id = cli.orchestrator.create_task("Fix the chess game", workflow_type="bugfix")
    success_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = success_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "src" / "ui").mkdir(parents=True)
    (source_dir / "src" / "ui" / "MenuScreen.ts").write_text("export {}\n", encoding="utf-8")
    success_path_md = success_dir / "success_path.md"
    success_path_md.write_text(f"success_path: {success_dir}\n", encoding="utf-8")
    cli.orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=f"{task_id}-success_path.md",
            task_id=task_id,
            phase_id=None,
            role="orchestrator",
            agent_id="harness",
            artifact_type="success_path.md",
            path=success_path_md,
            version=1,
            hash=None,
        )
    )

    context = cli._build_history_context(task_id)

    assert context
    assert f"- partial_materialized_source: {source_dir}" in context
    assert f"- materialized_source: {source_dir}" not in context


def test_history_context_recognizes_embedded_project_markers(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    task_id = cli.orchestrator.create_task("Fix embedded build", workflow_type="bugfix")
    success_dir = tmp_path / "deliver" / "project-embedded"
    source_dir = success_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "Kconfig").write_text("menuconfig TEST\n", encoding="utf-8")
    success_path_md = success_dir / "success_path.md"
    success_path_md.write_text(f"success_path: {success_dir}\n", encoding="utf-8")
    cli.orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=f"{task_id}-success_path.md",
            task_id=task_id,
            phase_id=None,
            role="orchestrator",
            agent_id="harness",
            artifact_type="success_path.md",
            path=success_path_md,
            version=1,
            hash=None,
        )
    )

    context = cli._build_history_context(task_id)

    assert context
    assert f"- materialized_source_candidate: {source_dir}" in context


def test_project_prompt_reuses_active_task_for_followup_dashboard(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = main_module.UiEventStore()
    cli = InteractiveCLI(config, "mock", ConsoleProgressReporter(), ui_store=store)
    historical_task_id = cli.orchestrator.create_task("Build a previous app")
    cli.active_task_id = historical_task_id

    monkeypatch.setattr(interactive_module, "classify_workflow_with_metadata", lambda prompt, backend, config=None: (_classification("new_project", 6), None))

    def fake_run_existing(
        orchestrator,
        task_id: str,
        prompt: str,
        workflow_type: str,
        project_context_md: str | None = None,
        classification: WorkflowClassification | None = None,
    ) -> int:
        assert task_id == historical_task_id
        assert classification and classification.workflow_type == "new_project"
        store(ProgressEvent("task_started", task_id=task_id, status="RUNNING"))
        return 0

    monkeypatch.setattr(interactive_module, "run_once", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("active follow-up must reuse task")))
    monkeypatch.setattr(interactive_module, "run_existing", fake_run_existing)

    cli._run_prompt("Build the next app")

    assert cli.active_task_id == historical_task_id
    assert store.latest_task_id == historical_task_id


def test_bare_continue_invokes_continue_command(monkeypatch, tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    calls: list[str] = []
    inputs = iter(["continue", "exit"])

    monkeypatch.setattr(cli, "_read_line", lambda: next(inputs))
    monkeypatch.setattr(cli, "_continue_task", lambda: calls.append("continue"))
    monkeypatch.setattr(cli, "_run_prompt", lambda prompt: calls.append(f"prompt:{prompt}"))

    assert cli.run() == 0
    assert calls == ["continue"]


def test_bare_continue_sentence_remains_prompt(monkeypatch, tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    calls: list[str] = []
    inputs = iter(["continue fixing BGM", "exit"])

    monkeypatch.setattr(cli, "_read_line", lambda: next(inputs))
    monkeypatch.setattr(cli, "_continue_task", lambda: calls.append("continue"))
    monkeypatch.setattr(cli, "_run_prompt", lambda prompt: calls.append(f"prompt:{prompt}"))

    assert cli.run() == 0
    assert calls == ["prompt:continue fixing BGM"]


def test_prompt_has_no_leading_newline(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())

    assert not cli._prompt().startswith("\n")
    assert cli._prompt().startswith("harness[mock]")


def test_read_line_uses_prompt_toolkit_session(monkeypatch, tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    prompts: list[str] = []

    class FakeTty:
        def isatty(self) -> bool:
            return True

    class FakePromptSession:
        def prompt(self, prompt: str) -> str:
            prompts.append(prompt)
            return "做一个天气软件"

    monkeypatch.setattr(interactive_module, "PROMPT_TOOLKIT_AVAILABLE", True)
    monkeypatch.setattr(main_module.sys, "stdin", FakeTty())
    monkeypatch.setattr(main_module.sys, "stdout", FakeTty())
    cli._prompt_session = FakePromptSession()

    value = cli._read_line()

    assert value == "做一个天气软件"
    assert prompts == ["harness[mock]> "]
    assert cli.input_history == ["做一个天气软件"]


def test_read_line_falls_back_to_standard_input_without_tty(monkeypatch, tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    prompts: list[str] = []

    class FakeStream:
        def isatty(self) -> bool:
            return False

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return "中文输入"

    monkeypatch.setattr(main_module.sys, "stdin", FakeStream())
    monkeypatch.setattr(main_module.sys, "stdout", FakeStream())
    monkeypatch.setattr(main_module.builtins, "input", fake_input)

    value = cli._read_line()

    assert value == "中文输入"
    assert prompts == ["harness[mock]> "]
    assert cli.input_history == ["中文输入"]


def test_read_line_returns_exit_on_non_tty_eof(monkeypatch, tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())

    class FakeStream:
        def isatty(self) -> bool:
            return False

    def fake_input(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(main_module.sys, "stdin", FakeStream())
    monkeypatch.setattr(main_module.sys, "stdout", FakeStream())
    monkeypatch.setattr(main_module.builtins, "input", fake_input)

    assert cli._read_line() == "exit"


def test_command_completion_items_include_live_command_candidates(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())

    items = cli.completion_items("/us")

    assert [item.text for item in items] == ["/use"]
    assert "Switch underlying agent backend" in str(items[0].display_meta)


def test_backend_completion_items_are_live_candidates(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())

    items = cli.completion_items("/use co")

    assert [item.text for item in items] == ["codex"]
    assert items[0].start_position == -2


def test_backend_completion_includes_gemini_and_qwen(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())

    assert [item.text for item in cli.completion_items("/use g")] == ["gemini"]
    assert [item.text for item in cli.completion_items("/use q")] == ["qwen"]
    assert [item.text for item in cli.completion_items("/go")] == ["/goal"]


def test_goal_command_sets_ten_fix_rounds(monkeypatch, tmp_path: Path, capsys) -> None:
    saved: dict[str, str] = {}
    monkeypatch.setattr(interactive_module, "save_user_env_value", lambda key, value: saved.update({key: value}))
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    cli.config["limits"]["max_test_fix_rounds"] = "unlimited"

    cli._handle_command("/goal")

    assert cli.config["limits"]["max_test_fix_rounds"] == 10
    assert saved == {"OO_MAX_TEST_FIX_ROUNDS": "10"}
    assert "goal max rounds: 10" in capsys.readouterr().out


def test_command_line_for_text_accepts_one_shot_slash_command(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())

    assert cli.command_line_for_text("/resume 1") == "/resume 1"
    assert cli.command_line_for_text("continue") == "/continue"
    assert cli.command_line_for_text("continue fixing BGM") is None


def test_one_shot_continue_accepts_task_selector(monkeypatch, tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    task_id = cli.orchestrator.create_task("Build a weather app")
    calls: list[str | None] = []
    monkeypatch.setattr(cli, "_continue_task", lambda: calls.append(cli.active_task_id))

    assert cli.run_command_once(f"/continue {task_id}") == 0

    assert calls == [task_id]


def test_main_dispatches_one_shot_slash_command(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("unused: true\n", encoding="utf-8")
    calls: list[str] = []

    def fake_load_config(path):
        assert str(path) == str(config_path)
        return _config(tmp_path)

    class FakeCLI:
        def __init__(self, *args, **kwargs):
            pass

        def command_line_for_text(self, text: str) -> str | None:
            return text if text.startswith("/") else None

        def run_command_once(self, command_line: str) -> int:
            calls.append(command_line)
            return 0

        def run(self):
            raise AssertionError("one-shot command should not enter interactive mode")

    monkeypatch.setattr(main_module, "load_config", fake_load_config)
    monkeypatch.setattr(main_module, "load_user_env", lambda path=main_module.USER_ENV_PATH: {"OO_BACKEND": "codex"})
    monkeypatch.setattr(main_module, "ensure_user_env_defaults", lambda config, values, path=main_module.USER_ENV_PATH: None)
    monkeypatch.setattr(main_module, "resolve_real_backend", lambda requested: requested)
    monkeypatch.setattr(main_module, "InteractiveCLI", FakeCLI)
    monkeypatch.setattr(main_module.sys, "argv", ["harness", "--config", str(config_path), "--no-ui", "/resume", "1"])

    assert main_module.main() == 0
    assert calls == ["/resume 1"]


def test_main_source_repo_argument_overrides_persistent_env(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("unused: true\n", encoding="utf-8")
    configured_source = tmp_path / "configured-source"
    cli_source = tmp_path / "cli-source"
    configured_source.mkdir()
    cli_source.mkdir()
    captured: dict[str, str] = {}

    def fake_load_config(path):
        assert str(path) == str(config_path)
        config = _config(tmp_path)
        config["system"]["source_repo"] = str(configured_source)
        return config

    def fake_run_once(orchestrator, prompt: str, workflow_type: str, classification=None) -> int:
        captured["prompt"] = prompt
        captured["workflow_type"] = workflow_type
        captured["source_repo"] = orchestrator.config["system"]["source_repo"]
        return 0

    monkeypatch.setattr(main_module, "load_config", fake_load_config)
    monkeypatch.setattr(
        main_module,
        "load_user_env",
        lambda path=main_module.USER_ENV_PATH: {"OO_BACKEND": "codex", "OO_SOURCE_REPO": str(configured_source)},
    )
    monkeypatch.setattr(main_module, "ensure_user_env_defaults", lambda config, values, path=main_module.USER_ENV_PATH: None)
    monkeypatch.setattr(main_module, "resolve_real_backend", lambda requested: requested)
    monkeypatch.setattr(main_module, "run_once", fake_run_once)
    monkeypatch.setattr(
        main_module.sys,
        "argv",
        [
            "harness",
            "--config",
            str(config_path),
            "--no-ui",
            "--workflow",
            "bugfix",
            "--source-repo",
            str(cli_source),
            "fix the bug",
        ],
    )

    assert main_module.main() == 0

    assert captured == {
        "prompt": "fix the bug",
        "workflow_type": "bugfix",
        "source_repo": str(cli_source.resolve()),
    }


def test_main_prompt_file_supplies_one_shot_prompt(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("unused: true\n", encoding="utf-8")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("fix from file\n", encoding="utf-8")
    captured: dict[str, str] = {}

    def fake_load_config(path):
        assert str(path) == str(config_path)
        return _config(tmp_path)

    def fake_run_once(orchestrator, prompt: str, workflow_type: str, classification=None) -> int:
        captured["prompt"] = prompt
        captured["workflow_type"] = workflow_type
        captured["fix_round_limit_callback"] = orchestrator.fix_round_limit_callback
        return 0

    monkeypatch.setattr(main_module, "load_config", fake_load_config)
    monkeypatch.setattr(main_module, "load_user_env", lambda path=main_module.USER_ENV_PATH: {"OO_BACKEND": "codex"})
    monkeypatch.setattr(main_module, "ensure_user_env_defaults", lambda config, values, path=main_module.USER_ENV_PATH: None)
    monkeypatch.setattr(main_module, "resolve_real_backend", lambda requested: requested)
    monkeypatch.setattr(main_module, "run_once", fake_run_once)
    monkeypatch.setattr(
        main_module.sys,
        "argv",
        [
            "harness",
            "--config",
            str(config_path),
            "--no-ui",
            "--workflow",
            "bugfix",
            "--prompt-file",
            str(prompt_file),
        ],
    )

    assert main_module.main() == 0

    assert captured == {"prompt": "fix from file", "workflow_type": "bugfix", "fix_round_limit_callback": None}


def test_main_rejects_prompt_file_with_extra_prompt_args(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("unused: true\n", encoding="utf-8")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("fix from file\n", encoding="utf-8")

    def fake_load_config(path):
        assert str(path) == str(config_path)
        return _config(tmp_path)

    monkeypatch.setattr(main_module, "load_config", fake_load_config)
    monkeypatch.setattr(main_module, "load_user_env", lambda path=main_module.USER_ENV_PATH: {"OO_BACKEND": "codex"})
    monkeypatch.setattr(main_module, "ensure_user_env_defaults", lambda config, values, path=main_module.USER_ENV_PATH: None)
    monkeypatch.setattr(main_module, "resolve_real_backend", lambda requested: requested)
    monkeypatch.setattr(main_module, "run_once", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run task")))
    monkeypatch.setattr(
        main_module.sys,
        "argv",
        [
            "harness",
            "--config",
            str(config_path),
            "--no-ui",
            "--prompt-file",
            str(prompt_file),
            "extra prompt",
        ],
    )

    assert main_module.main() == 2
    assert "--prompt-file cannot be combined" in capsys.readouterr().err


def test_main_rejects_empty_prompt_file(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("unused: true\n", encoding="utf-8")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(" \n", encoding="utf-8")

    def fake_load_config(path):
        assert str(path) == str(config_path)
        return _config(tmp_path)

    monkeypatch.setattr(main_module, "load_config", fake_load_config)
    monkeypatch.setattr(main_module, "load_user_env", lambda path=main_module.USER_ENV_PATH: {"OO_BACKEND": "codex"})
    monkeypatch.setattr(main_module, "ensure_user_env_defaults", lambda config, values, path=main_module.USER_ENV_PATH: None)
    monkeypatch.setattr(main_module, "resolve_real_backend", lambda requested: requested)
    monkeypatch.setattr(main_module, "run_once", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run task")))
    monkeypatch.setattr(
        main_module.sys,
        "argv",
        [
            "harness",
            "--config",
            str(config_path),
            "--no-ui",
            "--prompt-file",
            str(prompt_file),
        ],
    )

    assert main_module.main() == 2
    assert "--prompt-file must not be empty" in capsys.readouterr().err


def test_main_rejects_invalid_prompt_file_before_starting_ui(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("unused: true\n", encoding="utf-8")
    missing_prompt = tmp_path / "missing.md"

    def fake_load_config(path):
        assert str(path) == str(config_path)
        return _config(tmp_path)

    monkeypatch.setattr(main_module, "load_config", fake_load_config)
    monkeypatch.setattr(main_module, "load_user_env", lambda path=main_module.USER_ENV_PATH: {"OO_BACKEND": "codex"})
    monkeypatch.setattr(main_module, "ensure_user_env_defaults", lambda config, values, path=main_module.USER_ENV_PATH: None)
    monkeypatch.setattr(main_module, "resolve_real_backend", lambda requested: requested)
    monkeypatch.setattr(main_module, "start_ui_server", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ui should not start")))
    monkeypatch.setattr(
        main_module.sys,
        "argv",
        [
            "harness",
            "--config",
            str(config_path),
            "--prompt-file",
            str(missing_prompt),
        ],
    )

    assert main_module.main() == 2
    assert "--prompt-file must be an existing file" in capsys.readouterr().err


def test_main_cooldown_backend_persists_and_exits_without_ui(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("unused: true\n", encoding="utf-8")

    def fake_load_config(path):
        assert str(path) == str(config_path)
        return _config(tmp_path)

    monkeypatch.setattr(main_module, "load_config", fake_load_config)
    monkeypatch.setattr(main_module, "load_user_env", lambda path=main_module.USER_ENV_PATH: {"OO_BACKEND": "codex"})
    monkeypatch.setattr(main_module, "ensure_user_env_defaults", lambda config, values, path=main_module.USER_ENV_PATH: None)
    monkeypatch.setattr(main_module, "resolve_real_backend", lambda requested: requested)
    monkeypatch.setattr(main_module, "start_ui_server", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ui should not start")))
    monkeypatch.setattr(
        main_module.sys,
        "argv",
        ["harness", "--config", str(config_path), "--cooldown-backend", "claude", "30"],
    )

    assert main_module.main() == 0

    states = StateRepository(StateDB(tmp_path / "state" / "harness.db")).load_backend_health_states()
    assert states["claude"]["state"] == "open"
    assert states["claude"]["failure_kind"] == "manual_cooldown"
    assert "cooldown active for 30s" in capsys.readouterr().out


def test_resume_completion_items_include_task_information(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    task_id = cli.orchestrator.create_task("Build a weather app with IP lookup")
    cli.history_rows = cli.orchestrator.repository.list_tasks(20)

    suggestions = cli.completion_items("/resume ")

    assert suggestions
    assert suggestions[0].text == "1"
    assert task_id[:8] in str(suggestions[0].display_meta)
    assert "CREATED" in str(suggestions[0].display_meta)
    assert "Build a weather app" in str(suggestions[0].display_meta)


def test_display_truncation_preserves_wide_character_width() -> None:
    text = "做一个根据我IP来识别地区并且搜索天气的软件"

    truncated = main_module.truncate_display(text, 20)

    assert truncated.endswith("...")
    assert main_module.display_width(truncated) <= 20


def test_history_suggestions_align_chinese_prompt(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    cli.orchestrator.create_task("做一个根据我IP来识别地区并且搜索天气的软件")
    cli.history_rows = cli.orchestrator.repository.list_tasks(20)

    suggestion = str(cli.completion_items("/resume ")[0].display_meta)

    assert "做一个" in suggestion
    assert main_module.display_width(suggestion) <= 96


def test_dashboard_row_padding_uses_display_width(capsys) -> None:
    reporter = main_module.DashboardProgressReporter()
    reporter.enabled = False
    row = main_module.RoleView(
        role="执行者",
        status="COMPLETED",
        phase="阶段中文",
        agent_id="agent-1",
        artifacts=1,
    )

    reporter._render_row(row)

    output = capsys.readouterr().out.rstrip("\n")
    assert "执行者" in output
    assert main_module.display_width(output) == 110


def test_dashboard_render_does_not_draw_input_prompt(capsys) -> None:
    reporter = main_module.DashboardProgressReporter()
    reporter.enabled = False

    reporter._render()

    output = capsys.readouterr().out
    assert "Input:" not in output
    assert "harness>" not in output


def test_dashboard_render_counts_event_line(capsys) -> None:
    reporter = main_module.DashboardProgressReporter()
    reporter.enabled = False
    dashboard_line_count = len(reporter._dashboard_lines())

    reporter._render("phase started")
    assert reporter._rendered_lines == dashboard_line_count + 1

    reporter._render()
    assert reporter._rendered_lines == dashboard_line_count
    capsys.readouterr()


def test_user_env_round_trip(tmp_path: Path) -> None:
    env_path = tmp_path / ".openorchestra.env"

    main_module.save_user_env_value("OO_BACKEND", "claude", env_path)

    assert main_module.load_user_env(env_path)["OO_BACKEND"] == "claude"


def test_generated_docker_runtime_default_is_rewritten_to_config_default(tmp_path: Path) -> None:
    env_path = tmp_path / ".openorchestra.env"
    config = _config(tmp_path)
    config["testing"] = {"runtime": "auto"}

    main_module.ensure_user_env_defaults(config, {"OO_TEST_RUNTIME": "docker"}, env_path)

    assert main_module.load_user_env(env_path)["OO_TEST_RUNTIME"] == "auto"


def test_legacy_user_env_keys_are_mapped_to_openorchestra_keys(tmp_path: Path) -> None:
    env_path = tmp_path / ".myharness.env"
    env_path.write_text("HARNESS_BACKEND=claude\nHARNESS_TESTER_COUNT=3\n", encoding="utf-8")

    values = main_module.load_user_env(env_path)

    assert values["OO_BACKEND"] == "claude"
    assert values["OO_TESTER_COUNT"] == "3"


def test_ensure_user_env_defaults_adds_missing_config_values(tmp_path: Path) -> None:
    env_path = tmp_path / ".openorchestra.env"
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 2
    config["roles"]["executor"]["count"] = 2
    env_path.write_text("OO_BACKEND=claude\nOO_TESTER_COUNT=3\n", encoding="utf-8")

    main_module.ensure_user_env_defaults(config, main_module.load_user_env(env_path), env_path)

    values = main_module.load_user_env(env_path)
    assert values["OO_BACKEND"] == "claude"
    assert values["OO_PLANNER_COUNT"] == "2"
    assert values["OO_EXECUTOR_COUNT"] == "2"
    assert values["OO_TESTER_COUNT"] == "3"
    assert values["OO_REVIEWER_COUNT"] == "1"
    assert values["OO_JUDGE_COUNT"] == "1"
    assert values["OO_COMMUNICATOR_COUNT"] == "1"
    assert values["OO_WORKSPACE_ROOT"] == str(tmp_path / "workspaces")
    assert values["OO_UI_PORT"] == "8765"
    assert values["OO_CLAUDE_CONTEXT_WINDOW_TOKENS"] == "199999"
    assert values["OO_CLAUDE_MAX_TOKENS_MISC"] == "64000"
    assert values["OO_POLICY_SAME_ROLE_CAN_RUN_CONCURRENTLY"] == "true"


def test_ensure_user_env_defaults_migrates_legacy_generated_context_window_default(tmp_path: Path) -> None:
    env_path = tmp_path / ".openorchestra.env"
    config = _config(tmp_path)
    env_path.write_text(
        "\n".join(
            [
                "OO_CLAUDE_CONTEXT_WINDOW_TOKENS=200000",
                "OO_CLAUDE_MAX_TOKENS_EXECUTOR=48000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    main_module.ensure_user_env_defaults(config, main_module.load_user_env(env_path), env_path)

    values = main_module.load_user_env(env_path)
    assert values["OO_CLAUDE_CONTEXT_WINDOW_TOKENS"] == "199999"
    assert values["OO_CLAUDE_MAX_TOKENS_EXECUTOR"] == "48000"


def test_env_role_counts_override_config(tmp_path: Path) -> None:
    config = _config(tmp_path)

    main_module.apply_env_role_counts(
        config,
        {
            "OO_PLANNER_COUNT": "3",
            "OO_EXECUTOR_COUNT": "4",
            "OO_TESTER_COUNT": "2",
            "OO_REVIEWER_COUNT": "1",
        },
    )

    assert config["roles"]["planner"]["count"] == 3
    assert config["roles"]["executor"]["count"] == 4
    assert config["roles"]["tester"]["count"] == 2
    assert config["roles"]["reviewer"]["count"] == 1


def test_user_env_config_overrides_nested_values(tmp_path: Path) -> None:
    config = _config(tmp_path)

    main_module.apply_user_env_config(
        config,
        {
            "OO_BACKEND": "claude",
            "OO_WORKSPACE_ROOT": "/tmp/openorchestra-workspaces",
            "OO_PLANNER_COUNT": "4",
            "OO_TIMEOUT_PLANNER": "0",
            "OO_MAX_TEST_FIX_ROUNDS": "unlimited",
            "OO_UI_PORT": "9999",
            "OO_CLAUDE_MAX_TOKENS_EXECUTOR": "64000",
            "OO_POLICY_SAME_ROLE_CAN_RUN_CONCURRENTLY": "false",
        },
    )

    assert config["agent_backend"]["default"] == "claude"
    assert config["system"]["workspace_root"] == "/tmp/openorchestra-workspaces"
    assert config["roles"]["planner"]["count"] == 4
    assert config["timeouts"]["planner"] == 0
    assert config["limits"]["max_test_fix_rounds"] == "unlimited"
    assert config["visualization"]["port"] == 9999
    assert config["claude"]["max_output_tokens"]["executor"] == 64000
    assert config["policy"]["same_role_can_run_concurrently"] is False


def test_input_history_remembers_non_duplicate_commands(tmp_path: Path) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())

    cli._remember_input("/history")
    cli._remember_input("/history")
    cli._remember_input("/resume 1")

    assert cli.input_history == ["/history", "/resume 1"]


def test_interactive_cli_fix_round_limit_choices(monkeypatch, tmp_path: Path, capsys) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    answers = iter(["bad", "额外给10轮", "1", "退出", "2", "fix直至修复", "3"])
    monkeypatch.setattr(main_module.builtins, "input", lambda prompt: next(answers))

    assert cli._choose_test_fix_limit_action("task-1", 10) == "extra_10"
    assert cli._choose_test_fix_limit_action("task-1", 10) == "exit"
    assert cli._choose_test_fix_limit_action("task-1", 10) == "unlimited"
    output = capsys.readouterr().out
    assert "[WARN] 已达最大修复轮次(10)，任务终止。" in output
    assert output.count("请输入 1、2 或 3。") == 4


def test_main_starts_ui_by_default_and_can_disable_it(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("unused: true\n", encoding="utf-8")
    starts: list[str] = []

    def fake_load_config(path):
        assert str(path) == str(config_path)
        return _config(tmp_path)

    class FakeCLI:
        def __init__(self, config, backend, progress_callback, **kwargs):
            self.ui_server = kwargs.get("ui_server")

        def run(self):
            return 0

    monkeypatch.setattr(main_module, "load_config", fake_load_config)
    monkeypatch.setattr(main_module, "load_user_env", lambda path=main_module.USER_ENV_PATH: {"OO_BACKEND": "codex"})
    monkeypatch.setattr(main_module, "ensure_user_env_defaults", lambda config, values, path=main_module.USER_ENV_PATH: None)
    monkeypatch.setattr(main_module, "resolve_real_backend", lambda requested: requested)
    monkeypatch.setattr(main_module, "start_ui_server", lambda *args, **kwargs: starts.append("ui") or object())
    monkeypatch.setattr(main_module, "InteractiveCLI", FakeCLI)

    monkeypatch.setattr(main_module.sys, "argv", ["harness", "--config", str(config_path)])
    assert main_module.main() == 0
    assert starts == ["ui"]

    starts.clear()
    monkeypatch.setattr(main_module.sys, "argv", ["harness", "--config", str(config_path), "--no-ui"])
    assert main_module.main() == 0
    assert starts == []


def test_main_keyboard_interrupt_terminates_children_and_stops_ui(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("unused: true\n", encoding="utf-8")
    cleanup_calls: list[str] = []

    def fake_load_config(path):
        assert str(path) == str(config_path)
        return _config(tmp_path)

    class FakeServer:
        def stop(self):
            cleanup_calls.append("ui_stopped")

    class FakeCLI:
        def __init__(self, *args, **kwargs):
            self.ui_server = kwargs.get("ui_server")

        def run(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(main_module, "load_config", fake_load_config)
    monkeypatch.setattr(main_module, "load_user_env", lambda path=main_module.USER_ENV_PATH: {"OO_BACKEND": "codex"})
    monkeypatch.setattr(main_module, "ensure_user_env_defaults", lambda config, values, path=main_module.USER_ENV_PATH: None)
    monkeypatch.setattr(main_module, "resolve_real_backend", lambda requested: requested)
    monkeypatch.setattr(main_module, "InteractiveCLI", FakeCLI)
    monkeypatch.setattr(main_module, "start_ui_server", lambda *args, **kwargs: FakeServer())
    monkeypatch.setattr(main_module, "terminate_all_processes", lambda: cleanup_calls.append("children_terminated"))
    monkeypatch.setattr(main_module.sys, "argv", ["harness", "--config", str(config_path)])

    assert main_module.main() == 130

    assert cleanup_calls == ["children_terminated", "ui_stopped"]
    assert "active child processes were terminated" in capsys.readouterr().err


def test_clean_removes_selected_task_intermediate_files_and_keeps_success_path(tmp_path: Path, capsys) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    task_id = cli.orchestrator.create_task("Build a weather app")
    cli.active_task_id = task_id

    success_path = cli.orchestrator._delivery_project_dir(task_id, "Build a weather app")
    success_path.mkdir(parents=True)
    (success_path / "final_delivery.json").write_text("done", encoding="utf-8")
    (success_path / "success_path.md").write_text(f"success_path: {success_path}\n", encoding="utf-8")
    workspace_task_dir = Path(cli.config["system"]["workspace_root"]) / task_id
    artifact_task_dir = Path(cli.config["system"]["artifact_root"]) / task_id
    workspace_task_dir.mkdir(parents=True)
    artifact_task_dir.mkdir(parents=True)
    (workspace_task_dir / "prompt.md").write_text("prompt", encoding="utf-8")
    (artifact_task_dir / "patch.diff").write_text("patch", encoding="utf-8")

    cli._handle_command("/clean")

    output = capsys.readouterr().out
    assert "cleaned task:" in output
    assert "success_path:" in output
    assert not workspace_task_dir.exists()
    assert not artifact_task_dir.exists()
    assert (success_path / "final_delivery.json").exists()


def test_clean_refuses_without_final_success_path(tmp_path: Path, capsys) -> None:
    cli = InteractiveCLI(_config(tmp_path), "mock", ConsoleProgressReporter())
    task_id = cli.orchestrator.create_task("Build a weather app")
    cli.active_task_id = task_id
    workspace_task_dir = Path(cli.config["system"]["workspace_root"]) / task_id
    workspace_task_dir.mkdir(parents=True)
    (workspace_task_dir / "prompt.md").write_text("prompt", encoding="utf-8")

    cli._handle_command("/clean")

    output = capsys.readouterr().out
    assert "refusing to clean" in output
    assert workspace_task_dir.exists()


def test_dashboard_records_role_elapsed_seconds() -> None:
    reporter = main_module.DashboardProgressReporter()
    reporter.enabled = False

    reporter._apply(
        ProgressEvent(
            "agent_completed",
            task_id="task",
            phase="EXECUTION",
            role="executor",
            agent_id="executor-1",
            round_id=0,
            attempt=0,
            status="COMPLETED",
            data={"elapsed_seconds": 1.25},
        )
    )

    assert reporter.state.roles["executor"].elapsed_seconds == 1.25


def test_dashboard_tracks_individual_agent_rows() -> None:
    reporter = main_module.DashboardProgressReporter()
    reporter.enabled = False

    reporter._apply(
        ProgressEvent(
            "agent_completed",
            task_id="task",
            phase="PLANNING_DRAFT",
            role="planner",
            agent_id="planner-1",
            round_id=0,
            attempt=0,
            status="COMPLETED",
            data={"artifacts": 5, "elapsed_seconds": 1.0},
        )
    )
    reporter._apply(
        ProgressEvent(
            "agent_retryable_failure",
            task_id="task",
            phase="PLANNING_DRAFT",
            role="planner",
            agent_id="planner-2",
            round_id=0,
            attempt=1,
            status="OUTPUT_INVALID",
            data={"elapsed_seconds": 2.0},
        )
    )

    assert reporter.state.roles["planner"].artifacts == 5
    assert reporter.state.agents["planner:planner-1"].status == "COMPLETED"
    assert reporter.state.agents["planner:planner-1"].artifacts == 5
    assert reporter.state.agents["planner:planner-2"].status == "OUTPUT_INVALID"
    assert reporter.state.agents["planner:planner-2"].attempt == 2


def test_dashboard_role_summary_does_not_show_individual_agent_identity() -> None:
    reporter = main_module.DashboardProgressReporter()
    reporter.enabled = False

    reporter._apply(
        ProgressEvent(
            "agent_started",
            task_id="task",
            phase="PLANNING_DRAFT",
            role="planner",
            agent_id="planner-2",
            round_id=0,
            attempt=0,
            status="RUNNING",
        )
    )

    assert reporter.state.roles["planner"].agent_id == "-"
    assert reporter.state.roles["planner"].attempt is None
    assert reporter.state.agents["planner:planner-2"].agent_id == "planner-2"
    assert reporter.state.agents["planner:planner-2"].attempt == 1


def test_dashboard_resets_role_and_agent_state_for_new_task() -> None:
    reporter = main_module.DashboardProgressReporter()
    reporter.enabled = False
    reporter._apply(
        ProgressEvent(
            "agent_completed",
            task_id="task-1",
            phase="PLANNING_DRAFT",
            role="planner",
            agent_id="planner-1",
            status="COMPLETED",
            data={"artifacts": 5},
        )
    )

    reporter._apply(ProgressEvent("task_created", task_id="task-2", status="CREATED"))

    assert reporter.state.task_id == "task-2"
    assert reporter.state.roles["planner"].status == "PENDING"
    assert reporter.state.roles["planner"].artifacts == 0
    assert reporter.state.agents == {}


def test_dashboard_shows_running_status_when_task_starts() -> None:
    reporter = main_module.DashboardProgressReporter()
    reporter.enabled = False

    reporter._apply(ProgressEvent("task_started", task_id="task-1", status="RUNNING"))

    assert reporter.state.task_id == "task-1"
    assert reporter.state.task_status == "RUNNING"
