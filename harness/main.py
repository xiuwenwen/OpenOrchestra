from __future__ import annotations

import argparse
import builtins
import sys
from pathlib import Path

from harness.adapters.process_registry import terminate_all_processes
from harness.cli.interactive import InteractiveCLI
from harness.cli.runtime import REAL_BACKENDS, classify_workflow, resolve_real_backend, run_once
from harness.config.loader import load_config
from harness.config.user_env import (
    ENV_CONFIG_SPECS,
    LEGACY_ENV_ALIASES,
    LEGACY_USER_ENV_PATH,
    ROLE_COUNT_ENV_KEYS,
    USER_ENV_PATH,
    apply_env_role_counts,
    apply_user_env_config,
    canonicalize_user_env,
    ensure_user_env_defaults,
    get_nested_config,
    load_user_env,
    parse_env_value,
    save_user_env_value,
    set_nested_config,
    write_user_env,
)
from harness.core.misc_chat import MiscChatRunner
from harness.core.orchestrator import Orchestrator
from harness.core.progress import ProgressMultiplexer
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, MISC, NEW_PROJECT
from harness.delivery.handoff import DeliveryHandoff, format_delivery_handoff, format_total_elapsed
from harness.ui.display import display_width, pad_display, truncate_display
from harness.ui.launcher import start_ui_server
from harness.ui.server import UiEventStore
from harness.ui.terminal_dashboard import (
    ConsoleProgressReporter,
    DashboardProgressReporter,
    DashboardState,
    RoleView,
    make_progress_reporter,
)


def read_one_shot_prompt(args: argparse.Namespace) -> tuple[int, str]:
    if args.prompt_file and args.prompt:
        print("[ERROR] --prompt-file cannot be combined with prompt arguments", file=sys.stderr)
        return 2, ""
    if not args.prompt_file:
        return 0, " ".join(args.prompt).strip()

    prompt_file = Path(args.prompt_file).expanduser().resolve()
    if not prompt_file.exists() or not prompt_file.is_file():
        print(f"[ERROR] --prompt-file must be an existing file: {prompt_file}", file=sys.stderr)
        return 2, ""
    prompt = prompt_file.read_text(encoding="utf-8").strip()
    if not prompt:
        print(f"[ERROR] --prompt-file must not be empty: {prompt_file}", file=sys.stderr)
        return 2, ""
    return 0, prompt


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="orchestra",
        description="Run OpenOrchestra.",
        epilog=(
            "Slash commands also work as one-shot invocations, for example: "
            "orchestra /history 10, orchestra /resume 1, orchestra /continue <task_id>, "
            "orchestra diagnose <task_id>, orchestra /clean <task_id>, orchestra /goal."
        ),
    )
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
        "--source-repo",
        help=(
            "Use this existing project directory for the current run only. "
            "This overrides configured OO_SOURCE_REPO without writing ~/.openorchestra.env."
        ),
    )
    parser.add_argument(
        "--prompt-file",
        help="Read the one-shot task prompt from this UTF-8 text file instead of command-line arguments.",
    )
    parser.add_argument(
        "--test-runtime",
        choices=["auto", "native", "docker", "swebench"],
        help="Override test execution runtime for this invocation.",
    )
    parser.add_argument("--test-docker-image", help="Override the default Python Docker image for test execution.")
    parser.add_argument(
        "--docker-network",
        choices=["none", "install_only", "always", "default"],
        help="Docker network policy for test execution.",
    )
    parser.add_argument("--no-docker-test", action="store_true", help="Disable Docker test execution for this invocation.")
    parser.add_argument(
        "--ui",
        dest="ui",
        action="store_true",
        default=True,
        help="Start the local Web execution viewer. Enabled by default.",
    )
    parser.add_argument("--no-ui", dest="ui", action="store_false", help="Do not start the local Web execution viewer.")
    parser.add_argument("--ui-port", type=int, default=None, help="Port for the local Web execution viewer.")
    parser.add_argument(
        "--cooldown-backend",
        nargs=2,
        metavar=("BACKEND", "SECONDS"),
        help="Manually open a backend circuit for SECONDS, for example: --cooldown-backend claude 30.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    user_env = load_user_env()
    ensure_user_env_defaults(config, user_env)
    user_env = load_user_env()
    apply_user_env_config(config, user_env)
    if args.source_repo:
        source_repo = Path(args.source_repo).expanduser().resolve()
        if not source_repo.exists() or not source_repo.is_dir():
            print(f"[ERROR] --source-repo must be an existing directory: {source_repo}", file=sys.stderr)
            return 2
        config.setdefault("system", {})["source_repo"] = str(source_repo)
    if args.test_runtime:
        config.setdefault("testing", {})["runtime"] = args.test_runtime
    if args.test_docker_image:
        config.setdefault("testing", {}).setdefault("docker", {})["python_image"] = args.test_docker_image
    if args.docker_network:
        config.setdefault("testing", {}).setdefault("docker", {})["network"] = args.docker_network
    if args.no_docker_test:
        config.setdefault("testing", {}).setdefault("docker", {})["enabled"] = False
        if str(config.get("testing", {}).get("runtime") or "auto") == "docker":
            config["testing"]["runtime"] = "native"
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
    if args.cooldown_backend:
        backend_name, seconds_text = args.cooldown_backend
        try:
            cooldown_seconds = float(seconds_text)
        except ValueError:
            print(f"[ERROR] cooldown seconds must be numeric, got {seconds_text!r}", file=sys.stderr)
            return 2
        snapshot = orchestrator.backend_health.cooldown_backend(backend_name, cooldown_seconds)
        print(
            f"[backend] {snapshot.backend} cooldown active for {int(max(0.0, cooldown_seconds))}s "
            f"(open_until={snapshot.open_until})",
            flush=True,
        )
        return 0
    prompt_status, prompt = read_one_shot_prompt(args)
    if prompt_status:
        return prompt_status
    cli = InteractiveCLI(
        config,
        backend,
        progress_callback=progress_callback,
        default_workflow=args.workflow,
        ui_store=ui_store,
        ui_server=None,
        orchestrator=orchestrator,
        config_path=args.config,
    )
    try:
        if prompt:
            command_line = cli.command_line_for_text(prompt)
            if command_line:
                return cli.run_command_once(command_line)
            workflow_type, fallback_answer = (args.workflow, None) if args.workflow else classify_workflow(prompt, backend, config)
            if workflow_type == MISC:
                print(fallback_answer or MiscChatRunner(backend, config=config).ask(prompt))
                return 0
            if not args.workflow:
                print(f"[classifier] workflow_type={workflow_type}", flush=True)
            if args.ui:
                cli.ui_server = start_ui_server(config, orchestrator, ui_store, args.ui_port, args.config)
            # One-shot runs must fail closed at the fix-round limit instead of
            # waiting for the interactive /goal continuation prompt.
            orchestrator.fix_round_limit_callback = None
            return run_once(orchestrator, prompt, workflow_type)
        if args.ui:
            cli.ui_server = start_ui_server(config, orchestrator, ui_store, args.ui_port, args.config)
        return cli.run()
    except KeyboardInterrupt:
        terminate_all_processes()
        print("\n[interrupt] stopped; active child processes were terminated.", file=sys.stderr)
        return 130
    finally:
        ui_server = getattr(cli, "ui_server", None)
        if ui_server:
            stop = getattr(ui_server, "stop", None)
            if callable(stop):
                stop()


if __name__ == "__main__":
    raise SystemExit(main())
