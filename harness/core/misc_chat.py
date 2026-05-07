from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from harness.adapters.claude_config import claude_env_for_role, write_claude_invocation_settings
from harness.adapters.headless_cli_adapter import SUPPORTED_HEADLESS_CLI_BACKENDS, headless_cli_command
from harness.adapters.subprocess_runner import SubprocessRunner


class MiscChatError(RuntimeError):
    pass


class MiscChatRunner:
    def __init__(
        self,
        backend: str,
        runner: SubprocessRunner | None = None,
        log_root: Path | str = "logs/misc_chat",
        config: dict[str, Any] | None = None,
    ):
        self.backend = backend
        self.runner = runner or SubprocessRunner()
        self.log_root = Path(log_root)
        self.config = config or {}

    def ask(self, prompt: str, context: str | None = None, timeout_seconds: int = 0) -> str:
        run_dir = self.log_root / str(uuid.uuid4())
        run_dir.mkdir(parents=True, exist_ok=True)
        full_prompt = self._build_prompt(prompt, context)
        (run_dir / "prompt.md").write_text(full_prompt, encoding="utf-8")
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        env = claude_env_for_role(self.config, "misc", full_prompt) if self.backend == "claude" else None
        settings_path = write_claude_invocation_settings(run_dir, env or {}) if self.backend == "claude" else None
        command = self._command(run_dir, settings_path)
        (run_dir / "command.txt").write_text(" ".join(command), encoding="utf-8")
        if env:
            (run_dir / "env_overrides.txt").write_text(
                "\n".join(f"{key}={value}" for key, value in sorted(env.items())) + "\n",
                encoding="utf-8",
            )
        exit_code = self.runner.run(command, run_dir, timeout_seconds, stdout_path, stderr_path, input_text=full_prompt, env=env)
        if exit_code != 0:
            raise MiscChatError(f"Misc chat failed with exit_code={exit_code}. See logs: {run_dir}")
        return stdout_path.read_text(encoding="utf-8", errors="replace").strip()

    def _command(self, run_dir: Path, settings_path: Path | None = None) -> list[str]:
        if self.backend == "claude":
            command = ["claude", "-p"]
            if settings_path:
                command.extend(["--settings", str(settings_path)])
            command.extend(["--output-format", "text"])
            return command
        if self.backend == "codex":
            return ["codex", "exec", "--skip-git-repo-check", "--cd", str(run_dir), "-"]
        if self.backend in SUPPORTED_HEADLESS_CLI_BACKENDS:
            return headless_cli_command(self.backend)
        raise MiscChatError(f"Unsupported misc chat backend: {self.backend}")

    def _build_prompt(self, prompt: str, context: str | None) -> str:
        parts = [
            "# Direct Miscellaneous Response",
            "",
            "Answer the user's request directly.",
            "Do not modify files.",
            "Do not produce Harness artifacts.",
            "Be concise, practical, and grounded in the provided context when present.",
            "When the context contains concrete paths, use those exact paths instead of placeholders.",
            "Do not start long-running foreground commands such as dev servers; provide the command and URL instead.",
        ]
        if context:
            parts.extend(["", "## Context", context])
        parts.extend(["", "## User Request", prompt])
        return "\n".join(parts)
