from __future__ import annotations

from harness.adapters.base import AgentAdapter
from harness.adapters.runner_invocation import run_subprocess_runner
from harness.adapters.subprocess_runner import SubprocessRunner
from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult
from harness.prompts.builder import PromptBuilder


SUPPORTED_HEADLESS_CLI_BACKENDS = {"gemini", "qwen"}


def headless_cli_command(backend: str, *, prompt_mode: bool = True) -> list[str]:
    if backend not in SUPPORTED_HEADLESS_CLI_BACKENDS:
        raise ValueError(f"Unsupported headless CLI backend: {backend}")
    command = [backend]
    if prompt_mode:
        command.extend(["--prompt", ""])
    command.extend(["--output-format", "text"])
    if backend == "gemini":
        command.append("--skip-trust")
    if backend == "qwen":
        command.append("--bare")
    return command


class HeadlessCLIAdapter(AgentAdapter):
    def __init__(self, backend: str, command: list[str] | None = None, runner: SubprocessRunner | None = None):
        if backend not in SUPPORTED_HEADLESS_CLI_BACKENDS:
            raise ValueError(f"Unsupported headless CLI backend: {backend}")
        self.backend = backend
        self.command = command or headless_cli_command(backend)
        self.runner = runner or SubprocessRunner(stream_output=True, stream_prefix=f"[{backend}] ")
        self.prompt_builder = PromptBuilder()

    def run(self, context: AgentRunContext) -> AgentRunResult:
        prompt = self.prompt_builder.build(context)
        context.log_dir.mkdir(parents=True, exist_ok=True)
        (context.log_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        stdout_path = context.log_dir / "stdout.log"
        stderr_path = context.log_dir / "stderr.log"
        command = self.command + self._workspace_args(context) + self._auth_args(context) + self._approval_args(context)
        (context.log_dir / "command.txt").write_text(" ".join(command), encoding="utf-8")
        exit_code = run_subprocess_runner(
            self.runner,
            command,
            context.repo_dir,
            context.timeout_seconds,
            stdout_path,
            stderr_path,
            input_text=prompt,
            runtime_spec=context.runtime_spec,
        )
        return AgentRunResult(
            task_id=context.task_id,
            phase_id=context.phase_id,
            role=context.role,
            agent_id=context.agent_id,
            status="COMPLETED" if exit_code == 0 else "FAILED",
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    def _workspace_args(self, context: AgentRunContext) -> list[str]:
        return [
            "--include-directories",
            context.runtime_input_dir or str(context.input_dir),
            "--include-directories",
            context.runtime_output_dir or str(context.output_dir),
        ]

    def _approval_args(self, context: AgentRunContext) -> list[str]:
        backend_config = self._backend_config(context)
        approval_mode = backend_config.get("approval_mode", "yolo") if isinstance(backend_config, dict) else "yolo"
        if not approval_mode:
            return []
        if self.backend == "gemini" and approval_mode == "auto-edit":
            approval_mode = "auto_edit"
        if self.backend == "qwen" and approval_mode == "auto_edit":
            approval_mode = "auto-edit"
        return ["--approval-mode", str(approval_mode)]

    def _auth_args(self, context: AgentRunContext) -> list[str]:
        backend_config = self._backend_config(context)
        if not isinstance(backend_config, dict):
            return []
        auth_type = backend_config.get("auth_type")
        if not auth_type:
            return []
        args = ["--auth-type", str(auth_type)]
        openai_base_url = backend_config.get("openai_base_url")
        if self.backend == "qwen" and openai_base_url:
            args.extend(["--openai-base-url", str(openai_base_url)])
        return args

    def _backend_config(self, context: AgentRunContext) -> dict[str, object]:
        backend_config = context.config.get(self.backend, {}) if isinstance(context.config, dict) else {}
        return backend_config if isinstance(backend_config, dict) else {}
