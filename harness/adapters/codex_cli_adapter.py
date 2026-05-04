from __future__ import annotations

from harness.adapters.base import AgentAdapter
from harness.adapters.subprocess_runner import SubprocessRunner
from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult
from harness.prompts.builder import PromptBuilder


class CodexCLIAdapter(AgentAdapter):
    def __init__(self, command: list[str] | None = None, runner: SubprocessRunner | None = None):
        self.command = command or ["codex", "exec"]
        self.runner = runner or SubprocessRunner()
        self.prompt_builder = PromptBuilder()

    def run(self, context: AgentRunContext) -> AgentRunResult:
        prompt = self.prompt_builder.build(context)
        context.log_dir.mkdir(parents=True, exist_ok=True)
        (context.log_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        stdout_path = context.log_dir / "stdout.log"
        stderr_path = context.log_dir / "stderr.log"
        command = self.command + [
            "--skip-git-repo-check",
            "--cd",
            str(context.repo_dir),
            "--add-dir",
            str(context.input_dir),
            "--add-dir",
            str(context.output_dir),
            "-",
        ]
        (context.log_dir / "command.txt").write_text(" ".join(command), encoding="utf-8")
        exit_code = self.runner.run(
            command,
            context.repo_dir,
            context.timeout_seconds,
            stdout_path,
            stderr_path,
            input_text=prompt,
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
