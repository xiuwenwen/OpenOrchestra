from __future__ import annotations

from pathlib import Path

from harness.core.misc_chat import MiscChatRunner


class FakeRunner:
    def __init__(self, stdout: str = "answer"):
        self.stdout = stdout
        self.command: list[str] | None = None
        self.cwd: Path | None = None
        self.timeout_seconds: int | None = None
        self.env: dict[str, str] | None = None

    def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> int:
        self.command = command
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.env = env
        stdout_path.write_text(self.stdout, encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return 0


def test_misc_chat_runs_inside_its_log_directory_with_user_codex_sandbox(tmp_path: Path) -> None:
    runner = FakeRunner("direct answer")
    chat = MiscChatRunner("codex", runner=runner, log_root=tmp_path)

    answer = chat.ask("how do I use this?")

    assert answer == "direct answer"
    assert runner.cwd is not None
    assert runner.cwd.parent == tmp_path
    assert runner.command is not None
    assert "--sandbox" not in runner.command
    assert str(runner.cwd) in runner.command
    assert runner.timeout_seconds == 0


def test_misc_chat_applies_claude_token_budget(tmp_path: Path) -> None:
    runner = FakeRunner("direct answer")
    chat = MiscChatRunner(
        "claude",
        runner=runner,
        log_root=tmp_path,
        config={"claude": {"max_output_tokens": {"misc": 4321}}},
    )

    answer = chat.ask("how do I use this?")

    assert answer == "direct answer"
    assert runner.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "4321"}
    assert runner.cwd is not None
    assert (runner.cwd / "env_overrides.txt").read_text(encoding="utf-8") == "CLAUDE_CODE_MAX_OUTPUT_TOKENS=4321\n"
