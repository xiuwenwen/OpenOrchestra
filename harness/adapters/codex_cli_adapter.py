from __future__ import annotations

from pathlib import Path

from harness.adapters.base import AgentAdapter
from harness.adapters.claude_config import (
    ClaudeContextBudgetError,
    claude_context_window_tokens,
    claude_dynamic_max_output_tokens,
)
from harness.adapters.runner_invocation import run_subprocess_runner
from harness.adapters.subprocess_runner import SubprocessRunner
from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult
from harness.prompts.builder import PromptBuilder


class CodexCLIAdapter(AgentAdapter):
    def __init__(self, command: list[str] | None = None, runner: SubprocessRunner | None = None):
        self.command = command or ["codex", "exec"]
        self.runner = runner or SubprocessRunner(stream_output=True, stream_prefix="[codex] ")
        self.prompt_builder = PromptBuilder()

    def run(self, context: AgentRunContext) -> AgentRunResult:
        prompt = self.prompt_builder.build(context)
        context.log_dir.mkdir(parents=True, exist_ok=True)
        (context.log_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        stdout_path = context.log_dir / "stdout.log"
        stderr_path = context.log_dir / "stderr.log"
        try:
            max_output_tokens = claude_dynamic_max_output_tokens(context.config, context.role, prompt)
        except ClaudeContextBudgetError as exc:
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(f"{exc}\n", encoding="utf-8")
            diagnostics_path = self._write_context_budget_diagnostics(context, prompt, exc)
            with stderr_path.open("a", encoding="utf-8") as stderr:
                stderr.write(f"\nHarness request diagnostics: {diagnostics_path}\n")
            return AgentRunResult(
                task_id=context.task_id,
                phase_id=context.phase_id,
                role=context.role,
                agent_id=context.agent_id,
                status="FAILED",
                exit_code=1,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        command = self.command + self._config_args(context, max_output_tokens) + [
            "--skip-git-repo-check",
            "--cd",
            context.runtime_repo_dir or str(context.repo_dir),
            "--add-dir",
            context.runtime_input_dir or str(context.input_dir),
            "--add-dir",
            context.runtime_output_dir or str(context.output_dir),
            "-",
        ]
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

    def _config_args(self, context: AgentRunContext, max_output_tokens: int | None) -> list[str]:
        args: list[str] = []
        context_window = claude_context_window_tokens(context.config)
        if context_window is not None:
            args.extend(["-c", f"model_context_window={context_window}"])
        if max_output_tokens is not None:
            args.extend(["-c", f"max_output_tokens={max_output_tokens}"])
        return args

    def _write_context_budget_diagnostics(
        self,
        context: AgentRunContext,
        prompt: str,
        exc: ClaudeContextBudgetError,
    ) -> Path:
        diagnostics_path = context.log_dir / "request_diagnostics.md"
        lines = [
            "# Request Diagnostics",
            "",
            "## Run",
            f"- task_id: `{context.task_id}`",
            f"- phase: `{context.phase}`",
            f"- role: `{context.role}`",
            f"- agent_id: `{context.agent_id}`",
            "- backend: `codex`",
            "- exit_code: `not invoked`",
            f"- prompt_bytes: `{len(prompt.encode('utf-8'))}`",
            "",
            "## Error Signals",
            "- request_size_error_detected: `true`",
            "- preflight_context_budget_error: `true`",
            "- missing_required_outputs: `not checked`",
            "",
            "## Context Budget",
            f"- context_window_tokens: `{exc.context_window}`",
            f"- context_window_buffer_tokens: `{exc.buffer_tokens}`",
            f"- estimated_input_tokens: `{exc.estimated_input_tokens}`",
            f"- available_output_tokens: `{exc.available_output_tokens}`",
            f"- minimum_output_tokens: `{exc.minimum_output_tokens}`",
            "",
            "## Recommendation",
            "- Reduce staged artifact input, lower prompt size, or increase claude.context_window_tokens.",
            "",
        ]
        diagnostics_path.write_text("\n".join(lines), encoding="utf-8")
        return diagnostics_path
