from __future__ import annotations

import argparse
import builtins
import json
import os
import re
import shlex
import shutil
import sys
import threading
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
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

from harness.config.loader import load_config
from harness.core.misc_chat import MiscChatRunner
from harness.core.orchestrator import Orchestrator
from harness.core.progress import ProgressEvent, ProgressMultiplexer
from harness.core.workflow_classifier import WorkflowClassifier
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, MISC, NEW_PROJECT
from harness.ui.server import HarnessWebServer, UiEventStore
from harness.ui.terminal import TerminalStatusLine

USER_ENV_PATH = Path.home() / ".openorchestra.env"
LEGACY_USER_ENV_PATH = Path.home() / ".myharness.env"
REAL_BACKENDS = ("codex", "claude", "gemini", "qwen")


def display_width(text: str) -> int:
    width = 0
    for char in text:
        if char in {"\n", "\r"}:
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def truncate_display(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    if max_width <= 3:
        return "." * max_width
    current_width = 0
    chars: list[str] = []
    for char in text:
        char_width = display_width(char)
        if current_width + char_width > max_width - 3:
            return "".join(chars) + "..."
        chars.append(char)
        current_width += char_width
    return "".join(chars)


def pad_display(text: str, width: int) -> str:
    clipped = truncate_display(text, width)
    return clipped + (" " * max(0, width - display_width(clipped)))

ROLE_COUNT_ENV_KEYS = {
    "OO_PLANNER_COUNT": "planner",
    "OO_EXECUTOR_COUNT": "executor",
    "OO_TESTER_COUNT": "tester",
    "OO_REVIEWER_COUNT": "reviewer",
    "OO_JUDGE_COUNT": "judge",
    "OO_COMMUNICATOR_COUNT": "communicator",
}

ENV_CONFIG_SPECS: dict[str, tuple[tuple[str, ...], type]] = {
    "OO_BACKEND": (("agent_backend", "default"), str),
    "OO_WORKSPACE_ROOT": (("system", "workspace_root"), str),
    "OO_ARTIFACT_ROOT": (("system", "artifact_root"), str),
    "OO_DELIVER_ROOT": (("system", "deliver_root"), str),
    "OO_STATE_DB": (("system", "state_db"), str),
    "OO_SOURCE_REPO": (("system", "source_repo"), str),
    "OO_PLANNER_COUNT": (("roles", "planner", "count"), int),
    "OO_EXECUTOR_COUNT": (("roles", "executor", "count"), int),
    "OO_TESTER_COUNT": (("roles", "tester", "count"), int),
    "OO_REVIEWER_COUNT": (("roles", "reviewer", "count"), int),
    "OO_JUDGE_COUNT": (("roles", "judge", "count"), int),
    "OO_COMMUNICATOR_COUNT": (("roles", "communicator", "count"), int),
    "OO_MAX_PLANNING_ROUNDS": (("limits", "max_planning_rounds"), int),
    "OO_PLANNING_PEER_REVIEW_LOOPS": (("limits", "planning_peer_review_loops"), int),
    "OO_MAX_TEST_FIX_ROUNDS": (("limits", "max_test_fix_rounds"), str),
    "OO_MAX_REVIEW_ROUNDS": (("limits", "max_review_rounds"), int),
    "OO_MAX_AGENT_RETRY": (("limits", "max_agent_retry"), int),
    "OO_TIMEOUT_PLANNER": (("timeouts", "planner"), int),
    "OO_TIMEOUT_EXECUTOR": (("timeouts", "executor"), int),
    "OO_TIMEOUT_TESTER": (("timeouts", "tester"), int),
    "OO_TIMEOUT_REVIEWER": (("timeouts", "reviewer"), int),
    "OO_TIMEOUT_JUDGE": (("timeouts", "judge"), int),
    "OO_TIMEOUT_COMMUNICATOR": (("timeouts", "communicator"), int),
    "OO_HEARTBEAT_INTERVAL_SECONDS": (("heartbeat", "interval_seconds"), int),
    "OO_UI_HOST": (("visualization", "host"), str),
    "OO_UI_PORT": (("visualization", "port"), int),
    "OO_CLAUDE_MAX_TOKENS_CLASSIFIER": (("claude", "max_output_tokens", "classifier"), int),
    "OO_CLAUDE_MAX_TOKENS_MISC": (("claude", "max_output_tokens", "misc"), int),
    "OO_CLAUDE_MAX_TOKENS_PLANNER": (("claude", "max_output_tokens", "planner"), int),
    "OO_CLAUDE_MAX_TOKENS_EXECUTOR": (("claude", "max_output_tokens", "executor"), int),
    "OO_CLAUDE_MAX_TOKENS_TESTER": (("claude", "max_output_tokens", "tester"), int),
    "OO_CLAUDE_MAX_TOKENS_REVIEWER": (("claude", "max_output_tokens", "reviewer"), int),
    "OO_CLAUDE_MAX_TOKENS_JUDGE": (("claude", "max_output_tokens", "judge"), int),
    "OO_CLAUDE_MAX_TOKENS_COMMUNICATOR": (("claude", "max_output_tokens", "communicator"), int),
    "OO_CLAUDE_CONTEXT_WINDOW_TOKENS": (("claude", "context_window_tokens"), int),
    "OO_CLAUDE_CONTEXT_WINDOW_BUFFER_TOKENS": (("claude", "context_window_buffer_tokens"), int),
    "OO_ARTIFACT_INPUT_MAX_FILES": (("artifact_input", "max_files"), int),
    "OO_ARTIFACT_INPUT_MAX_FILE_BYTES": (("artifact_input", "max_file_bytes"), int),
    "OO_ARTIFACT_INPUT_MAX_TOTAL_BYTES": (("artifact_input", "max_total_bytes"), int),
    "OO_POLICY_DIFFERENT_ROLES_CAN_RUN_CONCURRENTLY": (("policy", "different_roles_can_run_concurrently"), bool),
    "OO_POLICY_SAME_ROLE_CAN_RUN_CONCURRENTLY": (("policy", "same_role_can_run_concurrently"), bool),
    "OO_POLICY_REQUIRE_JUDGE_FINAL_APPROVAL": (("policy", "require_judge_final_approval"), bool),
    "OO_POLICY_ALLOW_MEDIUM_BUG_DELIVERY": (("policy", "allow_medium_bug_delivery"), bool),
    "OO_POLICY_REQUIRE_ALL_TESTS_PASS": (("policy", "require_all_tests_pass"), bool),
}
LEGACY_ENV_ALIASES = {key.replace("OO_", "HARNESS_", 1): key for key in ENV_CONFIG_SPECS}


COMMANDS = {
    "/backend": "Show current backend",
    "/use": "Switch underlying agent backend",
    "/history": "List recent tasks",
    "/resume": "Use a historical task as context",
    "/continue": "Continue/retry the active historical task",
    "/clean": "Remove intermediate files for the selected task",
    "/goal": "Run test/fix loops until fixed without asking at the round limit",
    "/current": "Show selected historical context",
    "/clear": "Clear selected historical context",
    "/ui": "Start or show the local Web execution viewer",
    "/help": "Show command help",
    "/exit": "Quit",
    "/quit": "Quit",
}


COMMAND_ALIASES = {
    "/h": "/help",
    "/?": "/help",
    "/tasks": "/history",
    "/select": "/resume",
    "/task": "/resume",
    "/switch": "/use",
    "/retry": "/continue",
    "/run": "/continue",
}

BARE_COMMAND_ALIASES = {
    "help": "/help",
    "history": "/history",
    "tasks": "/history",
    "resume": "/resume",
    "select": "/resume",
    "task": "/resume",
    "continue": "/continue",
    "retry": "/continue",
    "run": "/continue",
    "clean": "/clean",
    "goal": "/goal",
    "current": "/current",
    "ui": "/ui",
}

BARE_COMMANDS_WITH_ARGS = {"history", "tasks", "resume", "select", "task"}
COMMAND_LINE_PATTERN = re.compile(
    r"^(?:python3?|\.\/|npm|pnpm|yarn|bun|uv|streamlit|flask|fastapi|uvicorn|node|deno|go|cargo|java|mvn|gradle|make|docker|docker-compose|bash|sh)\b"
)


class ConsoleProgressReporter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            line = self._format(event)
            print(line, flush=True)

    def _format(self, event: ProgressEvent) -> str:
        parts = ["[progress]", event.event_type]
        if event.phase:
            parts.append(f"phase={event.phase}")
        if event.role:
            parts.append(f"role={event.role}")
        if event.agent_id:
            parts.append(f"agent={event.agent_id}")
        if event.round_id is not None:
            parts.append(f"round={event.round_id}")
        if event.attempt is not None:
            parts.append(f"attempt={event.attempt + 1}")
        if event.status:
            parts.append(f"status={event.status}")
        for key in (
            "backend",
            "workspace",
            "output",
            "logs",
            "diagnostics",
            "artifacts",
            "result_type",
            "result_path",
            "success_path",
            "elapsed_seconds",
        ):
            if key in event.data:
                parts.append(f"{key}={event.data[key]}")
        if "delivery_status" in event.data:
            parts.append(f"delivery_status={event.data['delivery_status']}")
        if event.message:
            parts.append(f"- {event.message}")
        return " ".join(parts)


@dataclass
class RoleView:
    role: str
    status: str = "PENDING"
    phase: str = "-"
    round_id: int | None = None
    agent_id: str = "-"
    attempt: int | None = None
    artifacts: int = 0
    backend: str = "-"
    message: str = ""
    delivery_status: str = "-"
    elapsed_seconds: float | None = None


@dataclass
class DashboardState:
    task_id: str = "-"
    task_status: str = "-"
    current_phase: str = "-"
    backend: str = "-"
    workflow_type: str = "-"
    test_round: int = 0
    review_round: int = 0
    result_path: str = "-"
    success_path: str = "-"
    result_type: str = "-"
    roles: dict[str, RoleView] = field(default_factory=dict)
    agents: dict[str, RoleView] = field(default_factory=dict)
    events: deque[str] = field(default_factory=lambda: deque(maxlen=8))


@dataclass(frozen=True)
class DeliveryHandoff:
    project_dir: Path
    run_command: str | None
    dependency_install: str | None


class DashboardProgressReporter(ConsoleProgressReporter):
    ROLES = ("planner", "executor", "tester", "reviewer", "judge", "communicator", "orchestrator")

    def __init__(self) -> None:
        super().__init__()
        self.state = DashboardState()
        for role in self.ROLES:
            self.state.roles[role] = RoleView(role=role)
        self.enabled = sys.stdout.isatty()

    def __call__(self, event: ProgressEvent) -> None:
        if not self.enabled:
            return super().__call__(event)
        with self._lock:
            self._apply(event)
            self._render_event(event)

    def _apply(self, event: ProgressEvent) -> None:
        if event.event_type == "task_started" and event.task_id:
            self._reset_for_task(event.task_id)
        elif event.event_type == "task_created" and event.task_id and event.task_id != self.state.task_id:
            self._reset_for_task(event.task_id)
        self.state.task_id = event.task_id or self.state.task_id
        if event.phase:
            self.state.current_phase = event.phase
        if event.status:
            self.state.task_status = event.status if event.event_type.startswith("task_") else self.state.task_status
        
        # Handle skipped phases (resuming from checkpoint)
        if event.event_type == "phase_skipped":
            role = event.role
            if role and role in self.state.roles:
                row = self.state.roles[role]
                row.status = "COMPLETED"
                row.phase = event.phase
                row.round_id = event.round_id if event.round_id is not None else row.round_id
                row.message = event.message or "Skipped (already completed)"
            self.state.events.append(self._format(event))
            return

        if "backend" in event.data:
            self.state.backend = str(event.data["backend"])
        if "workflow_type" in event.data:
            self.state.workflow_type = str(event.data["workflow_type"])
        
        # Track rounds more comprehensively
        if event.phase in {"PLANNING_DRAFT", "PLANNING_PEER_REVIEW", "PLANNING_REVISION", "PLAN_REVIEW", "PLAN_JUDGEMENT", "TESTING", "TEST_JUDGEMENT", "FIXING", "PATCH_MERGE", "EXECUTION", "REGRESSION_TESTING", "REVIEW_FIXING"} and event.round_id is not None:
            self.state.test_round = event.round_id
        if event.phase in {"REVIEWING", "REVIEW_JUDGEMENT", "REVIEW_FIXING", "REGRESSION_TESTING"} and event.round_id is not None:
            self.state.review_round = event.round_id

        if "result_path" in event.data:
            self.state.result_path = str(event.data["result_path"])
        if "success_path" in event.data:
            self.state.success_path = str(event.data["success_path"])
        if "result_type" in event.data:
            self.state.result_type = str(event.data["result_type"])

        role = event.role
        if role and role in self.state.roles:
            row = self.state.roles[role]
            # For the role row, we prioritize phase events for status/phase/round updates
            is_phase_event = event.event_type.startswith("phase_")
            self._apply_row(
                row,
                event,
                aggregate_artifacts=True,
                update_status=is_phase_event or row.status == "PENDING",
                update_identity=False,
            )
            
            if event.agent_id:
                agent_key = f"{role}:{event.agent_id}"
                agent_row = self.state.agents.setdefault(agent_key, RoleView(role=f"  {event.agent_id}"))
                self._apply_row(agent_row, event, aggregate_artifacts=False, update_status=True)

        self.state.events.append(self._format(event))

    def _reset_for_task(self, task_id: str) -> None:
        self.state = DashboardState(task_id=task_id)
        for role in self.ROLES:
            self.state.roles[role] = RoleView(role=role)

    def _apply_row(
        self,
        row: RoleView,
        event: ProgressEvent,
        *,
        aggregate_artifacts: bool,
        update_status: bool = True,
        update_identity: bool = True,
    ) -> None:
        if update_status:
            row.status = event.status or row.status
            row.phase = event.phase or row.phase
            row.round_id = event.round_id if event.round_id is not None else row.round_id
            
        if update_identity:
            row.agent_id = event.agent_id or row.agent_id
            row.attempt = event.attempt + 1 if event.attempt is not None else row.attempt
        row.backend = str(event.data.get("backend", row.backend))
        if "artifacts" in event.data:
            if aggregate_artifacts:
                row.artifacts += int(event.data["artifacts"])
            else:
                row.artifacts = int(event.data["artifacts"])
        if "delivery_status" in event.data:
            row.delivery_status = str(event.data["delivery_status"])
        if "elapsed_seconds" in event.data and event.event_type in {"agent_completed", "agent_retryable_failure", "agent_failed", "phase_completed", "phase_failed", "agent_heartbeat"}:
            row.elapsed_seconds = float(event.data["elapsed_seconds"])
        row.message = event.message or row.message

    def _render(self) -> None:
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.write("OpenOrchestra Execution Dashboard\n")
        sys.stdout.write("=" * 88 + "\n")
        sys.stdout.write(f"Task: {self.state.task_id}\n")
        sys.stdout.write(
            f"Status: {self.state.task_status}   Phase: {self.state.current_phase}   "
            f"Backend: {self.state.backend}   Workflow: {self.state.workflow_type}\n"
        )
        sys.stdout.write(f"Test/Fix round: {self.state.test_round}   Review round: {self.state.review_round}\n")
        sys.stdout.write("-" * 88 + "\n")
        sys.stdout.write(f"{'Role':<14}{'Status':<14}{'Phase':<20}{'Round':<8}{'Agent':<18}{'Try':<6}{'Artifacts':<10}{'Delivery':<10}{'Elapsed':<10}\n")
        sys.stdout.write("-" * 88 + "\n")
        for role in self.ROLES:
            row = self.state.roles[role]
            self._render_row(row)
            for agent_row in self._agent_rows_for_role(role):
                self._render_row(agent_row)
        sys.stdout.write("-" * 88 + "\n")
        sys.stdout.write("Recent events:\n")
        for line in self.state.events:
            sys.stdout.write(f"  {truncate_display(line, 84)}\n")
        sys.stdout.write("-" * 88 + "\n")
        if self.state.result_type == "final_delivery" and self.state.result_path != "-":
            for line in format_delivery_handoff(Path(self.state.result_path)):
                sys.stdout.write(f"{line}\n")
        elif self.state.result_path != "-":
            sys.stdout.write(f"response: {self.state.result_path}\n")
        sys.stdout.flush()

    def _render_event(self, event: ProgressEvent) -> None:
        if event.event_type == "agent_heartbeat":
            TerminalStatusLine.write_status(self._compact_line(event, prefix="[running]"))
            return
        TerminalStatusLine.write_line(self._compact_line(event))

    def _compact_line(self, event: ProgressEvent, *, prefix: str | None = None) -> str:
        event_prefixes = {
            "task_created": "[task]",
            "task_started": "[task]",
            "task_completed": "[ok]",
            "task_failed": "[fail]",
            "phase_started": "[phase]",
            "phase_completed": "[ok]",
            "phase_failed": "[fail]",
            "phase_skipped": "[skip]",
            "agent_started": "[agent]",
            "agent_completed": "[ok]",
            "agent_retryable_failure": "[retry]",
            "agent_failed": "[fail]",
            "patch_validated": "[gate]",
        }
        parts = [prefix or event_prefixes.get(event.event_type, "[progress]")]
        if event.phase:
            parts.append(event.phase)
        if event.role:
            parts.append(event.role)
        if event.agent_id:
            parts.append(event.agent_id)
        if event.round_id is not None:
            parts.append(f"round={event.round_id}")
        if event.attempt is not None:
            parts.append(f"try={event.attempt + 1}")
        if event.status:
            parts.append(str(event.status))
        for key in ("workflow_type", "backend", "artifacts", "delivery_status", "elapsed_seconds"):
            if key in event.data:
                value = event.data[key]
                if key == "elapsed_seconds":
                    value = f"{float(value):.1f}s"
                parts.append(f"{key}={value}")
        if event.message:
            parts.append(f"- {event.message}")
        return truncate_display(" ".join(parts), 120)

    def _render_row(self, row: RoleView) -> None:
        round_text = "-" if row.round_id is None else str(row.round_id)
        attempt_text = "-" if row.attempt is None else str(row.attempt)
        elapsed_text = "-" if row.elapsed_seconds is None else f"{row.elapsed_seconds:.3f}s"
        sys.stdout.write(
            f"{pad_display(row.role, 14)}{pad_display(row.status, 14)}{pad_display(row.phase, 20)}{pad_display(round_text, 8)}"
            f"{pad_display(row.agent_id, 18)}{pad_display(attempt_text, 6)}{pad_display(str(row.artifacts), 10)}"
            f"{pad_display(row.delivery_status, 10)}{pad_display(elapsed_text, 10)}\n"
        )

    def _agent_rows_for_role(self, role: str) -> list[RoleView]:
        prefix = f"{role}:"
        return [self.state.agents[key] for key in sorted(self.state.agents) if key.startswith(prefix)]

    def _display_width(self, text: str) -> int:
        return display_width(text)


def make_progress_reporter() -> ConsoleProgressReporter:
    return DashboardProgressReporter() if sys.stdout.isatty() else ConsoleProgressReporter()


def main() -> int:
    parser = argparse.ArgumentParser(prog="orchestra", description="Run OpenOrchestra.")
    parser.add_argument("prompt", nargs="*", help="User task prompt to run through the harness")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--backend",
        choices=["auto", *REAL_BACKENDS],
        default=None,
        help="Real agent backend to use. auto prefers codex, then claude, gemini, qwen.",
    )
    parser.add_argument(
        "--serial-agents",
        action="store_true",
        help="Force one worker per multi-agent role. By default Harness respects configured role counts.",
    )
    parser.add_argument(
        "--workflow",
        choices=[BUGFIX, FEATURE_CHANGE, NEW_PROJECT, MISC],
        help="Override automatic workflow classification.",
    )
    parser.add_argument(
        "--ui",
        dest="ui",
        action="store_true",
        default=True,
        help="Start the local Web execution viewer. Enabled by default.",
    )
    parser.add_argument("--no-ui", dest="ui", action="store_false", help="Do not start the local Web execution viewer.")
    parser.add_argument("--ui-port", type=int, default=None, help="Port for the local Web execution viewer.")
    args = parser.parse_args()

    config = load_config(args.config)
    user_env = load_user_env()
    ensure_user_env_defaults(config, user_env)
    user_env = load_user_env()
    apply_user_env_config(config, user_env)
    backend = resolve_real_backend(args.backend or user_env.get("OO_BACKEND", "auto"))
    config["agent_backend"]["default"] = backend
    for role in ("planner", "executor", "tester", "reviewer", "judge", "communicator"):
        config["agent_backend"][role] = backend
    if args.serial_agents:
        for role in ("planner", "executor", "tester", "reviewer"):
            config["roles"][role]["count"] = 1
    progress_reporter = make_progress_reporter()
    ui_store = UiEventStore()
    progress_callback = ProgressMultiplexer([progress_reporter, ui_store])
    orchestrator = Orchestrator(config, progress_callback=progress_callback)
    ui_server = start_ui_server(config, orchestrator, ui_store, args.ui_port, args.config) if args.ui else None
    prompt = " ".join(args.prompt).strip()
    if prompt:
        workflow_type, fallback_answer = (args.workflow, None) if args.workflow else classify_workflow(prompt, backend, config)
        if workflow_type == MISC:
            print(fallback_answer or MiscChatRunner(backend, config=config).ask(prompt))
            return 0
        if not args.workflow:
            print(f"[classifier] workflow_type={workflow_type}", flush=True)
        return run_once(orchestrator, prompt, workflow_type)
    return InteractiveCLI(
        config,
        backend,
        progress_callback=progress_callback,
        default_workflow=args.workflow,
        ui_store=ui_store,
        ui_server=ui_server,
        orchestrator=orchestrator,
        config_path=args.config,
    ).run()


def start_ui_server(
    config: dict[str, Any],
    orchestrator: Orchestrator,
    ui_store: UiEventStore,
    port: int | None = None,
    config_path: str | Path | None = None,
) -> HarnessWebServer:
    visualization = config.get("visualization", {})
    server = HarnessWebServer(
        config,
        orchestrator.repository,
        ui_store,
        host=str(visualization.get("host", "127.0.0.1")),
        port=int(port if port is not None else visualization.get("port", 8765)),
        config_path=config_path,
    ).start()
    print(f"[ui] OpenOrchestra Execution Viewer: {server.url}")
    return server


def build_delivery_handoff(result_path: Path, usage_guide: Path | None = None) -> DeliveryHandoff:
    delivery_dir = result_path.parent
    project_dir = delivery_dir / "source" if (delivery_dir / "source").is_dir() else delivery_dir
    dependency_script = project_dir / "install_dependencies.sh"
    dependency_file = next(
        (path for path in (project_dir / "requirements.txt", project_dir / "request.txt") if path.exists()),
        None,
    )
    dependency_install = None
    if dependency_script.exists():
        dependency_install = f"cd {shlex.quote(str(project_dir))} && bash install_dependencies.sh"
    elif dependency_file:
        dependency_install = (
            f"cd {shlex.quote(str(project_dir))} && "
            f"python3 -m venv .venv && .venv/bin/python -m pip install -r {shlex.quote(dependency_file.name)}"
        )
    run_command = _delivery_run_command_for_environment(project_dir, result_path, usage_guide, dependency_script.exists())
    if dependency_script.exists() and run_command:
        run_command = _use_project_virtualenv_python(run_command)
    return DeliveryHandoff(project_dir=project_dir, run_command=run_command, dependency_install=dependency_install)


def format_delivery_handoff(result_path: Path, usage_guide: Path | None = None) -> list[str]:
    handoff = build_delivery_handoff(result_path, usage_guide)
    return [
        f"project_dir: {handoff.project_dir}",
        f"run_command: {handoff.run_command or 'not found in delivery docs'}",
        f"dependency_install: {handoff.dependency_install or 'none'}",
    ]


def format_total_elapsed(task: dict[str, Any] | None) -> str:
    elapsed = _task_elapsed_seconds(task)
    return f"total_elapsed: {_format_duration(elapsed)}" if elapsed is not None else "total_elapsed: unknown"


def _task_elapsed_seconds(task: dict[str, Any] | None) -> float | None:
    if not task:
        return None
    try:
        created_at = datetime.fromisoformat(str(task["created_at"]))
        updated_at = datetime.fromisoformat(str(task["updated_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return max(0.0, (updated_at - created_at).total_seconds())


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _first_delivery_run_command(project_dir: Path, result_path: Path, usage_guide: Path | None) -> str | None:
    for command in _delivery_run_commands(project_dir, result_path, usage_guide):
        return command
    return None


def _delivery_run_command_for_environment(
    project_dir: Path,
    result_path: Path,
    usage_guide: Path | None,
    has_dependency_installer: bool,
) -> str | None:
    for command in _delivery_run_commands(project_dir, result_path, usage_guide):
        command = _use_project_virtualenv_python(command) if has_dependency_installer else command
        if _delivery_command_is_executable(project_dir, command, has_dependency_installer):
            return command
    return _infer_executable_delivery_command(project_dir, has_dependency_installer)


def _delivery_run_commands(project_dir: Path, result_path: Path, usage_guide: Path | None) -> list[str]:
    delivery_dir = result_path.parent
    candidates = [
        usage_guide,
        delivery_dir / "usage_guide.md",
        project_dir / "README.md",
        project_dir / "readme.md",
        result_path,
    ]
    commands: list[str] = []
    for path in candidates:
        if not path or not path.exists() or not path.is_file():
            continue
        for command in _extract_shell_commands(path.read_text(encoding="utf-8", errors="replace")):
            if _is_dependency_install_command(command):
                continue
            commands.append(_with_project_cd(project_dir, command))
    return list(dict.fromkeys(commands))


def _infer_executable_delivery_command(project_dir: Path, has_dependency_installer: bool) -> str | None:
    if (project_dir / "package.json").exists():
        package = _read_json_file(project_dir / "package.json")
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        for script in ("dev", "start", "test"):
            if script in scripts and shutil.which("npm"):
                return _with_project_cd(project_dir, f"npm run {script}")
    python_bin = ".venv/bin/python" if has_dependency_installer else _available_python_command()
    if python_bin:
        for filename in ("app.py", "main.py"):
            if (project_dir / filename).exists():
                return _with_project_cd(project_dir, f"{python_bin} {filename}")
        if (project_dir / "tests").exists():
            return _with_project_cd(project_dir, f"{python_bin} -m pytest tests/")
    if (project_dir / "index.html").exists():
        return f"open {shlex.quote(str(project_dir / 'index.html'))}"
    return None


def _delivery_command_is_executable(project_dir: Path, command: str, has_dependency_installer: bool) -> bool:
    local_command = _strip_project_cd(command)
    try:
        parts = shlex.split(local_command)
    except ValueError:
        return False
    if not parts:
        return False
    executable = parts[0]
    if executable in {"python", "python3", ".venv/bin/python"}:
        if executable == ".venv/bin/python" and not has_dependency_installer and not (project_dir / executable).exists():
            return False
        if executable in {"python", "python3"} and shutil.which(executable) is None:
            return False
        return _python_command_target_exists(project_dir, parts)
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        return shutil.which(executable) is not None and (project_dir / "package.json").exists()
    if executable in {"node", "deno"}:
        return shutil.which(executable) is not None and len(parts) > 1 and (project_dir / parts[1]).exists()
    if executable in {"open"}:
        return len(parts) > 1 and Path(parts[1]).exists()
    if executable.startswith("./"):
        return os.access(project_dir / executable[2:], os.X_OK)
    return shutil.which(executable) is not None


def _python_command_target_exists(project_dir: Path, parts: list[str]) -> bool:
    if len(parts) >= 3 and parts[1] == "-m":
        module = parts[2]
        if module == "pytest":
            return (project_dir / "tests").exists() or any(project_dir.glob("test_*.py"))
        return True
    if len(parts) >= 2 and parts[1].endswith(".py"):
        return (project_dir / parts[1]).exists()
    return True


def _strip_project_cd(command: str) -> str:
    if " && " not in command:
        return command
    prefix, rest = command.split(" && ", 1)
    if prefix.startswith("cd "):
        return rest
    return command


def _available_python_command() -> str | None:
    for candidate in ("python3", "python"):
        if shutil.which(candidate):
            return candidate
    return None


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _extract_shell_commands(text: str) -> list[str]:
    commands: list[str] = []
    fenced_blocks = re.findall(r"```(?:bash|sh|shell|zsh|console|text)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    scan_texts = fenced_blocks or [text]
    for block in scan_texts:
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "$", ">")):
                line = line.lstrip("$> ").strip()
            if not line or line.startswith("#") or line.startswith("cd "):
                continue
            if COMMAND_LINE_PATTERN.match(line):
                commands.append(line)
    return commands


def _is_dependency_install_command(command: str) -> bool:
    lowered = command.lower()
    return "pip install" in lowered or "npm install" in lowered or "pnpm install" in lowered or "yarn install" in lowered


def _with_project_cd(project_dir: Path, command: str) -> str:
    if command.startswith("cd "):
        return command
    return f"cd {shlex.quote(str(project_dir))} && {command}"


def _use_project_virtualenv_python(command: str) -> str:
    return re.sub(r"(^|&& )python3?(?=\s)", r"\1.venv/bin/python", command, count=1)


def resolve_real_backend(requested: str) -> str:
    if requested == "auto":
        for candidate in REAL_BACKENDS:
            if shutil.which(candidate):
                return candidate
        raise RuntimeError("No real agent CLI found. Install one of: codex, claude, gemini, qwen.")
    if not shutil.which(requested):
        raise RuntimeError(f"Requested backend `{requested}` was not found on PATH.")
    return requested


def run_once(
    orchestrator: Orchestrator,
    prompt: str,
    workflow_type: str = NEW_PROJECT,
    project_context_md: str | None = None,
) -> int:
    task_id = orchestrator.create_task(prompt, workflow_type=workflow_type)
    if project_context_md:
        orchestrator.attach_project_context(task_id, project_context_md)
    result_path = orchestrator.run_task(task_id, workflow_type=workflow_type)
    if workflow_type == MISC:
        print(f"response: {Path(result_path)}")
    else:
        usage_guide = orchestrator.communicator.latest_usage_guide(task_id)
        for line in format_delivery_handoff(Path(result_path), usage_guide):
            print(line)
        print(format_total_elapsed(orchestrator.repository.get_task(task_id)))
    return 0


def load_user_env(path: Path = USER_ENV_PATH) -> dict[str, str]:
    if path == USER_ENV_PATH:
        values = _read_user_env_file(LEGACY_USER_ENV_PATH)
        values.update(_read_user_env_file(USER_ENV_PATH))
        return canonicalize_user_env(values)
    return canonicalize_user_env(_read_user_env_file(path))


def _read_user_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def canonicalize_user_env(values: dict[str, str]) -> dict[str, str]:
    canonical: dict[str, str] = {}
    for key, value in values.items():
        canonical[LEGACY_ENV_ALIASES.get(key, key)] = value
    return canonical


def save_user_env_value(key: str, value: str, path: Path = USER_ENV_PATH) -> None:
    values = load_user_env(path)
    values[key] = value
    write_user_env(values, path)


def write_user_env(values: dict[str, str], path: Path = USER_ENV_PATH) -> None:
    lines = ["# OpenOrchestra persistent CLI settings"]
    lines.extend(f"{name}={values[name]}" for name in sorted(values))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_user_env_defaults(config: dict[str, Any], values: dict[str, str], path: Path = USER_ENV_PATH) -> None:
    updated = dict(values)
    for key, (config_path, _value_type) in ENV_CONFIG_SPECS.items():
        if key not in updated:
            value = get_nested_config(config, config_path)
            if value is not None:
                updated[key] = str(value).lower() if isinstance(value, bool) else str(value)
    if updated != values:
        write_user_env(updated, path)


def apply_env_role_counts(config: dict[str, Any], values: dict[str, str]) -> None:
    for key, role in ROLE_COUNT_ENV_KEYS.items():
        if key not in values:
            continue
        try:
            count = int(values[key])
        except ValueError:
            continue
        if count > 0:
            config["roles"][role]["count"] = count


def apply_user_env_config(config: dict[str, Any], values: dict[str, str]) -> None:
    for key, (config_path, value_type) in ENV_CONFIG_SPECS.items():
        if key not in values:
            continue
        try:
            value = parse_env_value(values[key], value_type)
        except ValueError:
            continue
        if value_type is int and int(value) < 0:
            continue
        set_nested_config(config, config_path, value)


def parse_env_value(raw_value: str, value_type: type) -> Any:
    if value_type is bool:
        lowered = raw_value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Invalid boolean value: {raw_value}")
    if value_type is int:
        return int(raw_value)
    return raw_value


def get_nested_config(config: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def set_nested_config(config: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = config
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def classify_workflow(prompt: str, backend: str, config: dict[str, Any] | None = None) -> tuple[str, str | None]:
    workflow_type, _log_dir, fallback_answer = WorkflowClassifier(backend, config=config).classify_with_fallback(prompt)
    return workflow_type, fallback_answer


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
                command_line = self._bare_command_line(prompt)
                if command_line:
                    self._handle_command(command_line)
                elif prompt.startswith("/"):
                    self._handle_command(prompt)
                else:
                    self._run_prompt(prompt)
            except Exception as exc:
                print(f"task failed: {exc}", file=sys.stderr)
        return 0

    def _prompt(self) -> str:
        context = f" task={self.active_task_id[:8]}" if self.active_task_id else ""
        return f"harness[{self.backend}{context}]> "

    def _bare_command_line(self, text: str) -> str | None:
        parts = text.split()
        if not parts or parts[0].startswith("/"):
            return None
        token = parts[0].lower()
        command = BARE_COMMAND_ALIASES.get(token)
        if not command:
            return None
        if len(parts) > 1 and token not in BARE_COMMANDS_WITH_ARGS:
            return None
        return " ".join([command, *parts[1:]])

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
        canonical = COMMAND_ALIASES.get(command, command)
        description = COMMANDS.get(canonical, "")
        alias_note = f" -> {canonical}" if command != canonical else ""
        return f"{alias_note} {description}".strip()

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
            self._continue_task()
        elif command == "/clean":
            self._clean_task()
        elif command == "/goal":
            self._set_fix_until_goal()
        elif command in {"/resume", "/select", "/task"}:
            if not args:
                print("usage: /resume <history-number-or-task-id>")
                return
            self._resume_task(args[0])
        elif command == "/current":
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
                    canonical = COMMAND_ALIASES.get(match, match)
                    description = COMMANDS.get(canonical, "")
                    alias_note = f" -> {canonical}" if match != canonical else ""
                    print(f"  {match}{alias_note:<12} {description}")

    def _resolve_command(self, token: str) -> str:
        if token in COMMAND_ALIASES:
            return COMMAND_ALIASES[token]
        if token in COMMANDS:
            return token
        matches = [COMMAND_ALIASES.get(match, match) for match in self._matching_commands(token)]
        unique_matches = sorted(set(matches))
        if len(unique_matches) == 1:
            return unique_matches[0]
        return token

    def _matching_commands(self, prefix: str) -> list[str]:
        candidates = sorted([*COMMANDS.keys(), *COMMAND_ALIASES.keys()])
        return [command for command in candidates if command.startswith(prefix)]

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
                    "  /goal                    Set test/fix loops to fix until done without asking",
                    "  /current                 Show selected historical context",
                    "  /clear                   Clear selected historical context",
                    "  /ui                      Start/show the local Web execution viewer",
                    "  /help                    Show this help",
                    "  exit                     Quit",
                    "",
                    "Any non-command text starts a new task. If /resume is active, the historical task is included as reference context.",
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
        self.config.setdefault("limits", {})["max_test_fix_rounds"] = "unlimited"
        save_user_env_value("OO_MAX_TEST_FIX_ROUNDS", "unlimited")
        print(f"test/fix goal mode: fix until fixed (saved to {USER_ENV_PATH})")

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
        task_id = selector
        if selector.isdigit():
            if not self.history_rows:
                self._print_history(20)
            index = int(selector)
            if index < 1 or index > len(self.history_rows):
                print(f"history number out of range: {selector}")
                return
            task_id = self.history_rows[index - 1]["task_id"]
        task = self.orchestrator.repository.get_task(task_id)
        if not task:
            print(f"task not found: {task_id}")
            return
        self.active_task_id = task_id
        self.ui_store.select_task(task_id)
        print(f"resumed context: {task_id}")
        self._print_current()

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
        final_delivery = self._latest_artifact_path(self.active_task_id, "final_delivery.md")
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
                final_delivery = success_path / "final_delivery.md"
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
        effective_prompt = prompt
        project_context_md = None
        if self.active_task_id:
            project_context_md = self._build_project_context_md(self.active_task_id)
        previous_latest = self._latest_task_id()
        run_once(self.orchestrator, effective_prompt, workflow_type, project_context_md=project_context_md)
        latest_task_id = self._latest_task_id()
        if latest_task_id and latest_task_id != previous_latest:
            self.active_task_id = latest_task_id
            self.ui_store.select_task(latest_task_id)

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
            "final_delivery.md",
            "usage_guide.md",
            "success_path.md",
            "artifacts_manifest.md",
            "merged_patch.diff",
            "merged_patch_metadata.md",
            "merge_report.md",
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
        for artifact_type in ("final_delivery.md", "usage_guide.md", "response.md"):
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
            "merged_patch_metadata.md",
            "merge_report.md",
            "patch.diff",
            "fix_patch.diff",
        ):
            path = self._latest_artifact_path(task_id, artifact_type)
            if path:
                lines.append(f"Historical {artifact_type} path: {path}")
        final_delivery = self._latest_artifact_path(task_id, "final_delivery.md")
        if final_delivery:
            lines.extend(["", f"Historical final_delivery.md path: {final_delivery}"])
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
        final_delivery = self._latest_artifact_path(task_id, "final_delivery.md")
        return final_delivery.parent if final_delivery else None

    def _clean_task(self) -> None:
        if not self.active_task_id:
            print("no active historical context. Use /resume first.")
            return
        task = self.orchestrator.repository.get_task(self.active_task_id)
        if not task:
            print(f"active task no longer exists: {self.active_task_id}")
            self.active_task_id = None
            return

        success_path = self._success_path(self.active_task_id)
        if not success_path or not success_path.exists():
            print("no final success_path found; refusing to clean intermediate files")
            return
        final_delivery = success_path / "final_delivery.md"
        response = self._latest_artifact_path(self.active_task_id, "response.md")
        if not final_delivery.exists() and not response:
            print("no final delivery or response found; refusing to clean intermediate files")
            return

        workspace_task_dir = Path(self.config["system"]["workspace_root"]).expanduser().resolve() / self.active_task_id
        artifact_task_dir = Path(self.config["system"]["artifact_root"]).expanduser().resolve() / self.active_task_id
        candidates = [workspace_task_dir, artifact_task_dir]
        deleted: list[tuple[Path, int]] = []
        skipped: list[tuple[Path, str]] = []

        for path in candidates:
            if not path.exists():
                skipped.append((path, "not found"))
                continue
            if not self._is_safe_clean_target(path, self.active_task_id, success_path):
                skipped.append((path, "unsafe target"))
                continue
            size = self._path_size(path)
            shutil.rmtree(path)
            deleted.append((path, size))

        freed = sum(size for _, size in deleted)
        print(f"cleaned task: {self.active_task_id}")
        print(f"success_path: {success_path}")
        if final_delivery.exists():
            print(f"final_delivery: {final_delivery}")
        if deleted:
            for path, size in deleted:
                print(f"deleted: {path} ({self._format_bytes(size)})")
            print(f"freed: {self._format_bytes(freed)}")
        else:
            print("nothing deleted")
        for path, reason in skipped:
            print(f"skipped: {path} ({reason})")

    def _is_safe_clean_target(self, path: Path, task_id: str, success_path: Path) -> bool:
        resolved = path.resolve()
        if resolved.name != task_id:
            return False
        success_resolved = success_path.resolve()
        return not self._path_contains(resolved, success_resolved)

    def _path_contains(self, parent: Path, child: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    def _path_size(self, path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        total = 0
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
        return total

    def _format_bytes(self, size: int) -> str:
        units = ("B", "KB", "MB", "GB")
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024

    def _task_result_summary(self, task_id: str) -> str:
        success_path = self._success_path(task_id)
        if success_path and (success_path / "final_delivery.md").exists():
            return "delivery"
        if self._latest_artifact_path(task_id, "final_delivery.md"):
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


if __name__ == "__main__":
    raise SystemExit(main())
