from __future__ import annotations

import builtins
import json
import sys
import threading
from pathlib import Path
from typing import Any

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.document import Document
    from prompt_toolkit.history import InMemoryHistory

    PROMPT_TOOLKIT_AVAILABLE = True
except ModuleNotFoundError:
    PROMPT_TOOLKIT_AVAILABLE = False
    PromptSession = None

    class Completer:
        pass

    class Completion:
        def __init__(self, text: str, start_position: int = 0, display: str | None = None, display_meta: str | None = None):
            self.text = text
            self.start_position = start_position
            self.display = display
            self.display_meta = display_meta

    class Document:
        text_before_cursor = ""

    class InMemoryHistory:
        pass

from harness.cli.commands import bare_command_line, command_description, matching_commands, resolve_command
from harness.cli.runtime import REAL_BACKENDS, classify_workflow, resolve_real_backend, run_existing, run_once
from harness.config.user_env import USER_ENV_PATH, save_user_env_value
from harness.core.misc_chat import MiscChatRunner
from harness.core.orchestrator import Orchestrator
from harness.core.workflow_type import MISC, NEW_PROJECT
from harness.delivery.handoff import format_delivery_handoff, format_total_elapsed
from harness.diagnostics.service import DiagnosticsService
from harness.retention.service import RetentionService, format_bytes
from harness.ui.display import pad_display, truncate_display
from harness.ui.launcher import start_ui_server
from harness.ui.server import HarnessWebServer, UiEventStore


GOAL_MAX_TEST_FIX_ROUNDS = 10


class HarnessCompleter(Completer):
    def __init__(self, cli: "InteractiveCLI"):
        self.cli = cli

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        for item in self.cli.completion_items(text):
            yield item


class InteractiveCLI:
    def __init__(
        self,
        config: dict[str, Any],
        backend: str,
        progress_callback,
        default_workflow: str | None = None,
        ui_store: UiEventStore | None = None,
        ui_server: HarnessWebServer | None = None,
        orchestrator: Orchestrator | None = None,
        config_path: str | Path | None = None,
    ):
        self.config = config
        self.backend = backend
        self.progress_callback = progress_callback
        self.default_workflow = default_workflow
        self.ui_store = ui_store or UiEventStore()
        self.ui_server = ui_server
        self.config_path = config_path
        self.orchestrator = orchestrator or Orchestrator(self.config, progress_callback=self.progress_callback)
        self.orchestrator.fix_round_limit_callback = self._choose_test_fix_limit_action
        self.history_rows: list[dict[str, Any]] = []
        self.active_task_id: str | None = None
        self.input_history: list[str] = []
        self._stop_input = threading.Event()
        self._prompt_history = InMemoryHistory() if PROMPT_TOOLKIT_AVAILABLE else None
        self._prompt_session: Any | None = None
        self._prompt_toolkit_notice_shown = False

    def run(self) -> int:
        print("OpenOrchestra interactive mode. Type /help for commands, or 'exit' to quit.")
        while not self._stop_input.is_set():
            try:
                prompt = self._read_line().strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if not prompt:
                continue
            if prompt.lower() in {"exit", "quit", "q", "/exit", "/quit"}:
                return 0
            try:
                command_line = self.command_line_for_text(prompt)
                if command_line:
                    self._handle_command(command_line)
                else:
                    self._run_prompt(prompt)
            except Exception as exc:
                print(f"task failed: {exc}", file=sys.stderr)
        return 0

    def _prompt(self) -> str:
        context = f" task={self.active_task_id[:8]}" if self.active_task_id else ""
        return f"harness[{self.backend}{context}]> "

    def command_line_for_text(self, text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        if stripped.startswith("/"):
            return stripped
        return self._bare_command_line(stripped)

    def run_command_once(self, command_line: str) -> int:
        try:
            self._handle_command(command_line)
        except Exception as exc:
            print(f"task failed: {exc}", file=sys.stderr)
            return 1
        return 0

    def _bare_command_line(self, text: str) -> str | None:
        return bare_command_line(text)

    def _read_line(self) -> str:
        if not PROMPT_TOOLKIT_AVAILABLE:
            if sys.stdin.isatty() and sys.stdout.isatty() and not self._prompt_toolkit_notice_shown:
                print("[input] prompt_toolkit is not installed; live candidates are disabled.")
                print("[input] Install it with: python3 -m pip install prompt_toolkit")
                self._prompt_toolkit_notice_shown = True
            try:
                text = builtins.input(self._prompt())
            except EOFError:
                return "exit"
            self._remember_input(text)
            return text
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            try:
                text = builtins.input(self._prompt())
            except EOFError:
                return "exit"
            self._remember_input(text)
            return text
        text = self._get_prompt_session().prompt(self._prompt())
        self._remember_input(text)
        return text

    def _get_prompt_session(self) -> PromptSession:
        if self._prompt_session is None:
            self._prompt_session = PromptSession(
                completer=HarnessCompleter(self),
                history=self._prompt_history,
                complete_while_typing=True,
            )
        return self._prompt_session

    def _choose_test_fix_limit_action(self, task_id: str, current_limit: int) -> str:
        print(f"[WARN] 已达最大修复轮次({current_limit})，任务终止。")
        print("请选择下一步:")
        print("  1. 额外给10轮")
        print("  2. 退出")
        print("  3. fix直至修复")
        prompt = "选择 [1/2/3]: "
        while True:
            try:
                choice = builtins.input(prompt).strip()
            except EOFError:
                return "exit"
            if choice == "1":
                return "extra_10"
            if choice == "2":
                return "exit"
            if choice == "3":
                return "unlimited"
            print("请输入 1、2 或 3。")

    def completion_items(self, text: str) -> list[Completion]:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return []
        parts = stripped.split()
        if text.endswith(" "):
            parts.append("")
        if len(parts) == 1:
            matches = self._matching_commands(parts[0])
            return [
                Completion(
                    match,
                    start_position=-len(parts[0]),
                    display=match,
                    display_meta=self._command_description(match),
                )
                for match in matches[:8]
            ]
        command = self._resolve_command(parts[0].lower())
        current = parts[-1]
        if command == "/use" and len(parts) == 2:
            if current in REAL_BACKENDS:
                return []
            return [
                Completion(backend, start_position=-len(current), display=backend, display_meta="agent backend")
                for backend in REAL_BACKENDS
                if backend.startswith(current)
            ]
        if command == "/resume" and len(parts) == 2:
            if not self.history_rows:
                self.history_rows = self.orchestrator.repository.list_tasks(20)
            suggestions: list[Completion] = []
            for index, row in enumerate(self.history_rows, start=1):
                number = str(index)
                task_id = str(row["task_id"])
                if number.startswith(current):
                    suggestions.append(
                        Completion(number, start_position=-len(current), display=number, display_meta=self._format_history_meta(row))
                    )
                elif task_id.startswith(current):
                    suggestions.append(
                        Completion(task_id, start_position=-len(current), display=task_id, display_meta=self._format_history_meta(row))
                    )
            return suggestions[:8]
        if command == "/history" and len(parts) == 2:
            if current in {"5", "10", "20", "50"}:
                return []
            return [
                Completion(candidate, start_position=-len(current), display=candidate, display_meta="history limit")
                for candidate in ("5", "10", "20", "50")
                if candidate.startswith(current)
            ]
        return []

    def _command_description(self, command: str) -> str:
        return command_description(command)

    def _format_history_meta(self, row: dict[str, Any]) -> str:
        prompt = truncate_display(" ".join(str(row["user_prompt"]).split()), 40)
        result = self._task_result_summary(str(row["task_id"]))
        phase = row["current_phase"] or "-"
        return f"{str(row['task_id'])[:8]} {row['status']} {phase} {result} {prompt}"

    def _remember_input(self, text: str) -> None:
        if not text:
            return
        if not self.input_history or self.input_history[-1] != text:
            self.input_history.append(text)

    def _handle_command(self, command_line: str) -> None:
        parts = command_line.split()
        command = self._resolve_command(parts[0].lower())
        args = parts[1:]
        if command == "/help":
            self._print_help()
        elif command in {"/exit", "/quit"}:
            self._stop_input.set()
        elif command == "/backend":
            print(f"current backend: {self.backend}")
        elif command in {"/use", "/backend-use", "/switch"}:
            if not args:
                print("usage: /use codex|claude|gemini|qwen")
                return
            self._switch_backend(args[0])
        elif command in {"/history", "/tasks"}:
            limit = int(args[0]) if args and args[0].isdigit() else 20
            self._print_history(limit)
        elif command in {"/continue", "/retry", "/run"}:
            if args and not self._select_task_for_command(args[0]):
                return
            self._continue_task()
        elif command == "/clean":
            if args and not self._select_task_for_command(args[0]):
                return
            self._clean_task()
        elif command == "/diagnose":
            if args and not self._select_task_for_command(args[0]):
                return
            self._diagnose_task()
        elif command == "/goal":
            self._set_fix_until_goal()
        elif command in {"/resume", "/select", "/task"}:
            if not args:
                print("usage: /resume <history-number-or-task-id>")
                return
            self._resume_task(args[0])
        elif command == "/current":
            if args and not self._select_task_for_command(args[0]):
                return
            self._print_current()
        elif command == "/clear":
            self.active_task_id = None
            print("cleared active history context")
        elif command == "/ui":
            if not self.ui_server:
                self.ui_server = start_ui_server(self.config, self.orchestrator, self.ui_store, config_path=self.config_path)
            print(f"execution viewer: {self.ui_server.url}")
        else:
            print(f"unknown command: {command}. Type /help.")
            matches = self._matching_commands(command)
            if matches:
                print("matching commands:")
                for match in matches:
                    print(f"  {match:<16} {self._command_description(match)}")

    def _select_task_for_command(self, selector: str) -> bool:
        task_id = self._resolve_task_selector(selector)
        if not task_id:
            return False
        self.active_task_id = task_id
        self.ui_store.select_task(task_id)
        return True

    def _resolve_command(self, token: str) -> str:
        return resolve_command(token)

    def _matching_commands(self, prefix: str) -> list[str]:
        return matching_commands(prefix)

    def _print_help(self) -> None:
        print(
            "\n".join(
                [
                    "Commands:",
                    "  /backend                 Show current backend",
                    "  /use codex|claude|gemini|qwen",
                    "  /history [n]             List recent tasks",
                    "  /continue                Continue/retry the active historical task; bare 'continue' also works",
                    "  /resume <n|task_id>      Use a historical task as context for following prompts",
                    "  /clean                   Remove selected task workspaces/artifacts; keep final success_path",
                    "  /diagnose                Export prompt/log/artifact/event diagnostics for the active task",
                    "  /goal                    Set test/fix max rounds to 10",
                    "  /current                 Show selected historical context",
                    "  /clear                   Clear selected context; the next project request starts a new task",
                    "  /ui                      Start/show the local Web execution viewer",
                    "  /help                    Show this help",
                    "  exit                     Quit",
                    "",
                    "One-shot usage: `orchestra /resume 1`, `orchestra /continue <task_id>`, `orchestra diagnose <task_id>`.",
                    "The first non-command project request starts a task. Follow-up project requests reuse the active task; use /clear to start a separate task.",
                    "If /resume is active, ordinary questions use the historical task as chat context without creating a new workspace.",
                ]
            )
        )

    def _switch_backend(self, requested: str) -> None:
        backend = resolve_real_backend(requested)
        self.backend = backend
        self._apply_backend(backend)
        save_user_env_value("OO_BACKEND", backend)
        print(f"switched backend to: {backend} (saved to {USER_ENV_PATH})")

    def _set_fix_until_goal(self) -> None:
        self.config.setdefault("limits", {})["max_test_fix_rounds"] = GOAL_MAX_TEST_FIX_ROUNDS
        save_user_env_value("OO_MAX_TEST_FIX_ROUNDS", str(GOAL_MAX_TEST_FIX_ROUNDS))
        print(f"test/fix goal max rounds: {GOAL_MAX_TEST_FIX_ROUNDS} (saved to {USER_ENV_PATH})")

    def _apply_backend(self, backend: str) -> None:
        self.config["agent_backend"]["default"] = backend
        for role in ("planner", "executor", "tester", "reviewer", "judge", "communicator"):
            self.config["agent_backend"][role] = backend

    def _print_history(self, limit: int) -> None:
        self.history_rows = self.orchestrator.repository.list_tasks(limit)
        if not self.history_rows:
            print("no historical tasks")
            return
        print(f"{'#':>2}  {'task_id':<36}  {'status':<10} {'phase':<18} {'result':<15} {'created_at':<32} prompt")
        print("-" * 132)
        for index, row in enumerate(self.history_rows, start=1):
            prompt = " ".join(row["user_prompt"].split())
            prompt = truncate_display(prompt, 70)
            result = self._task_result_summary(row["task_id"])
            print(
                f"{index:>2}. {row['task_id']:<36}  {pad_display(str(row['status']), 10)} "
                f"{pad_display(str(row['current_phase'] or '-'), 18)} {pad_display(result, 15)} "
                f"{row['created_at']:<32} {prompt}"
            )

    def _resume_task(self, selector: str) -> None:
        task_id = self._resolve_task_selector(selector)
        if not task_id:
            return
        self.active_task_id = task_id
        self.ui_store.select_task(task_id)
        print(f"resumed context: {task_id}")
        self._print_current()

    def _resolve_task_selector(self, selector: str) -> str | None:
        task_id = selector
        if selector.isdigit():
            if not self.history_rows:
                self._print_history(20)
            index = int(selector)
            if index < 1 or index > len(self.history_rows):
                print(f"history number out of range: {selector}")
                return None
            task_id = self.history_rows[index - 1]["task_id"]
        task = self.orchestrator.repository.get_task(task_id)
        if not task:
            print(f"task not found: {task_id}")
            return None
        return str(task_id)

    def _continue_task(self) -> None:
        if not self.active_task_id:
            print("no active historical context. Use /resume first.")
            return
        task = self.orchestrator.repository.get_task(self.active_task_id)
        if not task:
            print(f"active task no longer exists: {self.active_task_id}")
            self.active_task_id = None
            return
        workflow_type = task.get("workflow_type") or NEW_PROJECT
        self.ui_store.select_task(self.active_task_id)
        task_workspace = Path(self.config["system"]["workspace_root"]).expanduser().resolve() / self.active_task_id
        print(f"continuing task: {self.active_task_id} (workflow: {workflow_type})")
        print(f"task_workspace: {task_workspace}")
        result_path = self.orchestrator.run_task(self.active_task_id, workflow_type=workflow_type)
        if workflow_type != MISC:
            usage_guide = self._latest_artifact_path(self.active_task_id, "usage_guide.md")
            for line in format_delivery_handoff(Path(result_path), usage_guide):
                print(line)
            print(format_total_elapsed(self.orchestrator.repository.get_task(self.active_task_id)))

    def _print_current(self) -> None:
        if not self.active_task_id:
            print("no active historical context")
            return
        task = self.orchestrator.repository.get_task(self.active_task_id)
        if not task:
            print(f"active task no longer exists: {self.active_task_id}")
            self.active_task_id = None
            return
        final_delivery = self._latest_artifact_path(self.active_task_id, "final_delivery.json")
        print(f"task_id: {task['task_id']}")
        print(f"status: {task['status']}")
        print(f"phase: {task['current_phase']}")
        print(f"prompt: {task['user_prompt']}")
        if final_delivery:
            print(f"final_delivery: {final_delivery}")
            success_path = final_delivery.parent
            print(f"success_path: {success_path}")
        else:
            success_path = self._success_path(self.active_task_id)
            if success_path:
                final_delivery = success_path / "final_delivery.json"
                if final_delivery.exists():
                    print(f"final_delivery: {final_delivery}")
                print(f"success_path: {success_path}")
        usage_guide = self._latest_artifact_path(self.active_task_id, "usage_guide.md")
        if usage_guide:
            print(f"usage_guide: {usage_guide}")
        response = self._latest_artifact_path(self.active_task_id, "response.md")
        if response:
            print(f"response: {response}")

    def _run_prompt(self, prompt: str) -> None:
        if self.active_task_id and not self.default_workflow and self._active_context_prompt_is_misc(prompt):
            context = self._build_history_context(self.active_task_id)
            print(MiscChatRunner(self.backend, config=self.config).ask(prompt, context=context))
            return
        workflow_type, fallback_answer = (
            (self.default_workflow, None)
            if self.default_workflow
            else classify_workflow(prompt, self.backend, self.config)
        )
        if workflow_type == MISC:
            context = self._build_history_context(self.active_task_id) if self.active_task_id else None
            print(fallback_answer or MiscChatRunner(self.backend, config=self.config).ask(prompt, context=context))
            return
        if not self.default_workflow:
            print(f"[classifier] workflow_type={workflow_type}", flush=True)
        project_context_md = None
        if self.active_task_id:
            project_context_md = self._build_project_context_md(self.active_task_id)
            run_existing(
                self.orchestrator,
                self.active_task_id,
                prompt,
                workflow_type,
                project_context_md=project_context_md,
            )
            self.ui_store.select_task(self.active_task_id)
            return
        previous_latest = self._latest_task_id()
        run_once(self.orchestrator, prompt, workflow_type, project_context_md=project_context_md)
        latest_task_id = self._latest_task_id()
        if latest_task_id and latest_task_id != previous_latest:
            self.active_task_id = latest_task_id
            self.ui_store.select_task(latest_task_id)

    def _active_context_prompt_is_misc(self, prompt: str) -> bool:
        text = prompt.strip().lower()
        if not text:
            return False
        change_markers = (
            "fix",
            "repair",
            "modify",
            "change",
            "add ",
            "implement",
            "build",
            "create",
            "delete",
            "remove",
            "refactor",
            "修复",
            "修改",
            "新增",
            "添加",
            "实现",
            "开发",
            "重构",
            "删除",
            "移除",
        )
        if any(marker in text for marker in change_markers):
            return False
        question_markers = (
            "?",
            "？",
            "how",
            "why",
            "what",
            "where",
            "when",
            "explain",
            "show",
            "tell me",
            "怎么",
            "如何",
            "为什么",
            "为何",
            "什么",
            "哪里",
            "在哪",
            "解释",
            "说明",
            "看下",
            "看看",
            "启动",
            "运行",
            "路径",
            "日志",
        )
        return any(marker in text for marker in question_markers)

    def _build_history_context(self, task_id: str | None) -> str | None:
        if not task_id:
            return None
        task = self.orchestrator.repository.get_task(task_id)
        if not task:
            return None
        task_workspace = Path(self.config["system"]["workspace_root"]).expanduser().resolve() / task_id
        success_path = self._success_path(task_id)
        latest_repo = self._latest_agent_repo_path(task_id)
        lines = [
            f"Historical task id: {task['task_id']}",
            f"Historical workflow type: {task.get('workflow_type') or '-'}",
            f"Historical status: {task['status']}",
            f"Historical current phase: {task.get('current_phase') or '-'}",
            f"Historical current role: {task.get('current_role') or '-'}",
            f"Historical prompt: {task['user_prompt']}",
            "",
            "Historical concrete paths:",
            f"- task_workspace: {task_workspace}",
        ]
        if success_path:
            lines.append(f"- success_path: {success_path}")
            source_path = success_path / "source"
            if source_path.exists():
                source_label = (
                    "materialized_source_candidate"
                    if self._looks_like_runnable_source(source_path)
                    else "partial_materialized_source"
                )
                lines.append(f"- {source_label}: {source_path}")
                lines.append("- source_note: reconstructed from patch; validate completeness with final.patch and project tests.")
        if latest_repo:
            lines.append(f"- latest_agent_repo_workspace: {latest_repo}")
        for artifact_type in (
            "final_delivery.json",
            "usage_guide.md",
            "success_path.md",
            "artifacts_manifest.md",
            "merged_patch.diff",
            "merged_patch_metadata.json",
            "patch.diff",
            "fix_patch.diff",
            "response.md",
        ):
            path = self._latest_artifact_path(task_id, artifact_type)
            if path:
                lines.append(f"- {artifact_type}: {path}")
        lines.extend(
            [
                "",
                "When answering how to start, run, inspect, or apply this historical task, use the concrete paths above.",
                "Do not replace known concrete paths with placeholders such as /path/to/merged_patch.diff.",
            ]
        )
        for artifact_type in ("final_delivery.json", "usage_guide.md", "response.md"):
            path = self._latest_artifact_path(task_id, artifact_type)
            if path:
                lines.extend(["", f"Historical {artifact_type} path: {path}", self._read_excerpt(path)])
        return "\n".join(lines)

    def _build_project_context_md(self, task_id: str) -> str | None:
        task = self.orchestrator.repository.get_task(task_id)
        if not task:
            return None
        lines = [
            "# Project Context",
            "",
            f"Historical task id: {task['task_id']}",
            f"Historical workflow type: {task.get('workflow_type') or '-'}",
            f"Historical status: {task['status']}",
            f"Historical current phase: {task.get('current_phase') or '-'}",
            f"Historical current role: {task.get('current_role') or '-'}",
            f"Historical prompt: {task['user_prompt']}",
        ]
        task_workspace = Path(self.config["system"]["workspace_root"]).expanduser().resolve() / task_id
        lines.extend(["", f"Historical task_workspace: {task_workspace}"])
        success_path = self._success_path(task_id)
        if success_path:
            lines.append(f"Historical success_path: {success_path}")
            source_path = success_path / "source"
            if source_path.exists():
                lines.append(f"Historical source_repo: {source_path}")
        latest_repo = self._latest_agent_repo_path(task_id)
        if latest_repo:
            lines.append(f"Historical latest_agent_repo_workspace: {latest_repo}")
        for artifact_type in (
            "merged_patch.diff",
            "merged_patch_metadata.json",
            "patch.diff",
            "fix_patch.diff",
        ):
            path = self._latest_artifact_path(task_id, artifact_type)
            if path:
                lines.append(f"Historical {artifact_type} path: {path}")
        final_delivery = self._latest_artifact_path(task_id, "final_delivery.json")
        if final_delivery:
            lines.extend(["", f"Historical final_delivery.json path: {final_delivery}"])
            lines.extend(["Historical final delivery excerpt:", self._read_excerpt(final_delivery)])
        decisions = self.orchestrator.repository.list_judge_decisions(task_id)[-3:]
        if decisions:
            lines.append("")
            lines.append("Recent historical judge decisions:")
            for decision in decisions:
                payload = self._compact_json(decision["decision_payload"])
                lines.append(f"- {decision['decision_type']}: {payload}")
        return "\n".join(lines)

    def _latest_artifact_path(self, task_id: str, artifact_type: str) -> Path | None:
        artifacts = self.orchestrator.repository.list_artifacts(task_id, artifact_type)
        for artifact in reversed(artifacts):
            path = Path(artifact["path"])
            if path.exists() and path.is_file():
                return path
        return None

    def _looks_like_runnable_source(self, path: Path) -> bool:
        project_markers = (
            "package.json",
            "pyproject.toml",
            "setup.py",
            "requirements.txt",
            "Cargo.toml",
            "go.mod",
            "pom.xml",
            "build.gradle",
            "CMakeLists.txt",
            "Makefile",
            "Kconfig",
            "Kbuild",
            "meson.build",
            "BUILD",
            "WORKSPACE",
            "configure",
            "index.html",
        )
        return any((path / marker).exists() for marker in project_markers)

    def _latest_agent_repo_path(self, task_id: str) -> Path | None:
        phases = {phase["phase_id"]: phase for phase in self.orchestrator.repository.list_phases(task_id)}
        workspace_root = Path(self.config["system"]["workspace_root"]).expanduser().resolve()
        for run in reversed(self.orchestrator.repository.list_agent_runs(task_id)):
            phase = phases.get(run["phase_id"], {})
            round_id = int(phase.get("round_id") or 0)
            repo_path = (
                workspace_root
                / task_id
                / str(run["phase_id"])
                / str(run["role"])
                / str(run["agent_id"])
                / f"round_{round_id}"
                / f"attempt_{run['retry_count']}"
                / "repo"
            )
            if repo_path.exists() and repo_path.is_dir():
                return repo_path
        return None

    def _latest_task_id(self) -> str | None:
        tasks = self.orchestrator.repository.list_tasks(1)
        return str(tasks[0]["task_id"]) if tasks else None

    def _success_path(self, task_id: str) -> Path | None:
        success_path_md = self._latest_artifact_path(task_id, "success_path.md")
        if success_path_md:
            return success_path_md.parent
        success_path = self.orchestrator.delivery_success_path(task_id)
        if success_path:
            return success_path
        final_delivery = self._latest_artifact_path(task_id, "final_delivery.json")
        return final_delivery.parent if final_delivery else None

    def _clean_task(self) -> None:
        if not self.active_task_id:
            print("no active historical context. Use /resume first.")
            return
        plan = RetentionService(
            config=self.config,
            repository=self.orchestrator.repository,
            success_path_provider=self.orchestrator.delivery_success_path,
        ).clean_task(self.active_task_id)
        if not plan.deleted and plan.skipped and plan.skipped[0].reason.startswith("no final"):
            print(f"{plan.skipped[0].reason}; refusing to clean intermediate files")
            return
        if not plan.deleted and plan.skipped and plan.skipped[0].reason == "task is active":
            print("task is active; refusing to clean intermediate files")
            return
        print(f"cleaned task: {self.active_task_id}")
        if plan.success_path:
            print(f"success_path: {plan.success_path}")
        if plan.final_delivery and plan.final_delivery.exists():
            print(f"final_delivery: {plan.final_delivery}")
        if plan.deleted:
            for action in plan.deleted:
                print(f"deleted: {action.path} ({format_bytes(action.size)})")
            print(f"freed: {format_bytes(plan.freed_bytes)}")
        else:
            print("nothing deleted")
        for action in plan.skipped:
            print(f"skipped: {action.path} ({action.reason})")

    def _diagnose_task(self) -> None:
        if not self.active_task_id:
            print("no active historical context. Use /resume first.")
            return
        try:
            bundle = DiagnosticsService(config=self.config, repository=self.orchestrator.repository).export_task(self.active_task_id)
        except KeyError:
            print(f"active task no longer exists: {self.active_task_id}")
            self.active_task_id = None
            return
        print(f"diagnostics_bundle: {bundle.path}")
        print(f"copied_evidence_files: {bundle.copied_files}")

    def _task_result_summary(self, task_id: str) -> str:
        success_path = self._success_path(task_id)
        if success_path and (success_path / "final_delivery.json").exists():
            return "delivery"
        if self._latest_artifact_path(task_id, "final_delivery.json"):
            return "delivery"
        if self._latest_artifact_path(task_id, "response.md"):
            return "response"
        if self._latest_artifact_path(task_id, "merged_patch.diff"):
            return "merged_patch"
        if self._latest_artifact_path(task_id, "patch.diff"):
            return "patch"
        return "-"

    def _read_excerpt(self, path: Path, max_chars: int = 2000) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"[artifact unavailable: {exc}]"
        return text[:max_chars] + ("\n...[truncated]" if len(text) > max_chars else "")

    def _compact_json(self, payload: str, max_chars: int = 500) -> str:
        try:
            text = json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":"))
        except json.JSONDecodeError:
            text = payload
        return text[:max_chars] + ("..." if len(text) > max_chars else "")
