from __future__ import annotations

from pathlib import Path

from harness.adapters.claude_code_adapter import ClaudeCodeAdapter
from harness.adapters.claude_config import ClaudeContextBudgetError, claude_dynamic_max_output_tokens, estimate_prompt_tokens
from harness.agents.context import AgentRunContext


class FakeRunner:
    def __init__(self):
        self.command: list[str] | None = None
        self.env: dict[str, str] | None = None

    def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: int | None,
        stdout_path: Path,
        stderr_path: Path,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> int:
        self.command = command
        self.env = env
        stdout_path.write_text("ok", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return 0


class RequestTooLargeRunner(FakeRunner):
    def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: int | None,
        stdout_path: Path,
        stderr_path: Path,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> int:
        self.command = command
        self.env = env
        stdout_path.write_text("Request too large (max 32MB). Try with a smaller file.", encoding="utf-8")
        stderr_path.write_text("request_too_large: budget_exceeds_model_limit", encoding="utf-8")
        return 1


def _context(tmp_path: Path, config: dict, role: str = "planner") -> AgentRunContext:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    input_dir = workspace / "input"
    output_dir = workspace / "output"
    logs = workspace / "logs"
    for path in (repo, input_dir, output_dir, logs):
        path.mkdir(parents=True, exist_ok=True)
    return AgentRunContext(
        task_id="task",
        phase_id="phase",
        phase="PLANNING_DRAFT",
        role=role,
        agent_id=f"{role}-1",
        round_id=0,
        user_prompt="plan",
        role_instruction="plan",
        workspace_dir=workspace,
        repo_dir=repo,
        input_dir=input_dir,
        output_dir=output_dir,
        log_dir=logs,
        required_outputs=["plan.md", "delivery.md"],
        timeout_seconds=0,
        config=config,
    )


def test_claude_adapter_does_not_force_permission_mode(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(_context(tmp_path, config={}))

    assert runner.command is not None
    assert "--permission-mode" not in runner.command
    assert runner.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}


def test_claude_adapter_accepts_configured_permission_mode(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(tmp_path, config={"claude": {"permission_mode": "bypassPermissions"}})
    )

    assert runner.command is not None
    assert runner.command[runner.command.index("--permission-mode") + 1] == "bypassPermissions"


def test_claude_adapter_uses_configured_max_output_tokens(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(tmp_path, config={"claude": {"max_output_tokens": 8192}})
    )

    assert runner.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8192"}


def test_claude_dynamic_max_output_tokens_respects_context_window() -> None:
    prompt = "x" * 4000

    adjusted = claude_dynamic_max_output_tokens(
        {
            "claude": {
                "context_window_tokens": 2000,
                "context_window_buffer_tokens": 0,
                "max_output_tokens": 1500,
            }
        },
        "planner",
        prompt,
    )

    assert estimate_prompt_tokens(prompt) == 1000
    assert adjusted == 1000


def test_claude_adapter_lowers_max_output_for_large_prompt(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(
            tmp_path,
            config={
                "claude": {
                    "context_window_tokens": 3000,
                    "context_window_buffer_tokens": 1000,
                    "max_output_tokens": 1000,
                }
            },
        )
    )

    assert runner.env is not None
    assert int(runner.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]) < 1000


def test_claude_dynamic_max_output_tokens_raises_when_prompt_exceeds_budget() -> None:
    prompt = "x" * 4000

    try:
        claude_dynamic_max_output_tokens(
            {
                "claude": {
                    "context_window_tokens": 1000,
                    "context_window_buffer_tokens": 0,
                    "max_output_tokens": 500,
                }
            },
            "planner",
            prompt,
        )
    except ClaudeContextBudgetError as exc:
        assert exc.available_output_tokens == 0
        assert exc.estimated_input_tokens == 1000
    else:
        raise AssertionError("Expected ClaudeContextBudgetError")


def test_claude_adapter_fails_preflight_without_invoking_runner_for_oversized_prompt(tmp_path: Path) -> None:
    runner = FakeRunner()

    result = ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(
            tmp_path,
            config={
                "claude": {
                    "context_window_tokens": 1000,
                    "context_window_buffer_tokens": 0,
                    "max_output_tokens": 500,
                }
            },
        )
    )

    assert result.status == "FAILED"
    assert runner.command is None
    diagnostics = tmp_path / "workspace" / "logs" / "request_diagnostics.md"
    assert diagnostics.exists()
    assert "preflight_context_budget_error: `true`" in diagnostics.read_text(encoding="utf-8")


def test_claude_adapter_uses_role_specific_max_output_tokens(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(
            tmp_path,
            role="executor",
            config={"claude": {"max_output_tokens": {"planner": 24000, "executor": 48000, "judge": 12000}}},
        )
    )

    assert runner.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "48000"}


def test_claude_adapter_uses_role_specific_default_when_mapping_omits_role(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(tmp_path, role="communicator", config={"claude": {"max_output_tokens": {"judge": 12000}}})
    )

    assert runner.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}


def test_claude_adapter_can_disable_max_output_override(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(tmp_path, config={"claude": {"max_output_tokens": 0}})
    )

    assert runner.env == {}


def test_claude_adapter_writes_request_diagnostics_for_oversized_requests(tmp_path: Path) -> None:
    runner = RequestTooLargeRunner()
    context = _context(tmp_path, config={}, role="executor")

    result = ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(context)

    diagnostics = context.log_dir / "request_diagnostics.md"
    stderr = context.log_dir / "stderr.log"
    assert result.status == "FAILED"
    assert diagnostics.exists()
    diagnostics_text = diagnostics.read_text(encoding="utf-8")
    assert "request_size_error_detected: `true`" in diagnostics_text
    assert "missing_required_outputs" in diagnostics_text
    assert "Request too large" in diagnostics_text
    assert "Harness request diagnostics:" in stderr.read_text(encoding="utf-8")
