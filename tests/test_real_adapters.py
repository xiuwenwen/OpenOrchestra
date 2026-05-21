from __future__ import annotations

import json
from pathlib import Path

from harness.adapters.claude_code_adapter import ClaudeCodeAdapter
from harness.adapters.claude_config import (
    DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE,
    MIN_DYNAMIC_MAX_OUTPUT_TOKENS,
    TOKEN_ESTIMATE_SAFETY_MULTIPLIER,
    ClaudeContextBudgetError,
    claude_dynamic_max_output_tokens,
    estimate_prompt_tokens,
)
from harness.adapters.codex_cli_adapter import CodexCLIAdapter
from harness.adapters.headless_cli_adapter import HeadlessCLIAdapter
from harness.agents.context import AgentRunContext
from harness.runtime.spec import RuntimeSpec


class FakeRunner:
    def __init__(self):
        self.command: list[str] | None = None
        self.env: dict[str, str] | None = None
        self.input_text: str | None = None

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
        self.input_text = input_text
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


class RuntimeAwareFakeRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.runtime_spec: RuntimeSpec | None = None

    def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: int | None,
        stdout_path: Path,
        stderr_path: Path,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        runtime_spec: RuntimeSpec | None = None,
    ) -> int:
        self.runtime_spec = runtime_spec
        return super().run(command, cwd, timeout_seconds, stdout_path, stderr_path, input_text=input_text, env=env)


def _ascii_prompt_for_estimated_tokens(token_count: int) -> str:
    return "x" * (token_count * 4)


def _available_output_tokens(prompt: str) -> int:
    return DEFAULT_CONTEXT_WINDOW_TOKENS - DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS - estimate_prompt_tokens(prompt)


def _context(tmp_path: Path, config: dict, role: str = "planner", user_prompt: str = "plan") -> AgentRunContext:
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
        user_prompt=user_prompt,
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


def _docker_context(tmp_path: Path, config: dict, role: str = "planner") -> AgentRunContext:
    context = _context(tmp_path, config, role=role)
    return AgentRunContext(
        **{
            **context.__dict__,
            "runtime_spec": RuntimeSpec(mode="docker", image="agent:latest", workdir="/workspace"),
            "runtime_workspace_dir": "/openorchestra",
            "runtime_repo_dir": "/workspace",
            "runtime_input_dir": "/openorchestra/input",
            "runtime_output_dir": "/openorchestra/output",
            "runtime_log_dir": "/openorchestra/logs",
        }
    )


def test_claude_adapter_does_not_force_permission_mode(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(_context(tmp_path, config={}))

    assert runner.command is not None
    assert "--permission-mode" not in runner.command
    assert runner.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}
    assert "--settings" in runner.command
    settings_path = Path(runner.command[runner.command.index("--settings") + 1])
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}
    }


def test_claude_adapter_uses_runtime_paths_in_docker_mode(tmp_path: Path) -> None:
    runner = RuntimeAwareFakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(_docker_context(tmp_path, config={}))

    assert runner.command is not None
    assert runner.runtime_spec is not None and runner.runtime_spec.is_docker
    assert runner.command[runner.command.index("--settings") + 1].startswith("/openorchestra/logs/")
    assert runner.command[runner.command.index("--add-dir") + 1] == "/openorchestra/input"
    assert runner.command[runner.command.index("--add-dir") + 2] == "/openorchestra/output"


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
    assert runner.command is not None
    settings_path = Path(runner.command[runner.command.index("--settings") + 1])
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8192"}
    }


def test_claude_dynamic_max_output_tokens_respects_context_window() -> None:
    prompt = _ascii_prompt_for_estimated_tokens(145_000)
    estimated_input_tokens = estimate_prompt_tokens(prompt)
    expected_available_output_tokens = _available_output_tokens(prompt)

    adjusted = claude_dynamic_max_output_tokens(
        {
            "claude": {
                "context_window_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
                "context_window_buffer_tokens": DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS,
                "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE["planner"],
            }
        },
        "planner",
        prompt,
    )

    assert MIN_DYNAMIC_MAX_OUTPUT_TOKENS <= expected_available_output_tokens
    assert expected_available_output_tokens < DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE["planner"]
    assert adjusted == expected_available_output_tokens


def test_claude_adapter_lowers_max_output_for_large_prompt(tmp_path: Path) -> None:
    runner = FakeRunner()
    user_prompt = _ascii_prompt_for_estimated_tokens(135_000)

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(
            tmp_path,
            config={
                "claude": {
                    "context_window_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
                    "context_window_buffer_tokens": DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS,
                    "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE["planner"],
                }
            },
            user_prompt=user_prompt,
        )
    )

    assert runner.env is not None
    adjusted_max_output_tokens = int(runner.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"])
    assert MIN_DYNAMIC_MAX_OUTPUT_TOKENS <= adjusted_max_output_tokens
    assert adjusted_max_output_tokens < DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE["planner"]
    assert runner.command is not None
    settings_path = Path(runner.command[runner.command.index("--settings") + 1])
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(adjusted_max_output_tokens)}
    }


def test_dynamic_max_output_tokens_keeps_128k_requests_below_200k_context() -> None:
    prompt = _ascii_prompt_for_estimated_tokens(72_001)

    adjusted = claude_dynamic_max_output_tokens(
        {
            "claude": {
                "context_window_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
                "context_window_buffer_tokens": DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS,
                "max_output_tokens": 128_000,
            }
        },
        "planner",
        prompt,
    )

    assert adjusted == _available_output_tokens(prompt)
    assert adjusted < 128_000


def test_prompt_token_estimate_uses_conservative_mixed_language_multiplier() -> None:
    prompt = "修复 bug and update README with 12345\n" * 100
    cjk_estimate_without_safety = 200
    ascii_bytes = len((" bug and update README with 12345\n" * 100).encode("utf-8"))
    lower_bound = int((cjk_estimate_without_safety + ascii_bytes / 4) * TOKEN_ESTIMATE_SAFETY_MULTIPLIER)

    assert estimate_prompt_tokens(prompt) >= lower_bound
    assert TOKEN_ESTIMATE_SAFETY_MULTIPLIER >= 1.30


def test_claude_dynamic_max_output_tokens_raises_when_prompt_exceeds_budget() -> None:
    prompt = _ascii_prompt_for_estimated_tokens(DEFAULT_CONTEXT_WINDOW_TOKENS)
    estimated_input_tokens = estimate_prompt_tokens(prompt)
    expected_available_output_tokens = _available_output_tokens(prompt)

    try:
        claude_dynamic_max_output_tokens(
            {
                "claude": {
                    "context_window_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
                    "context_window_buffer_tokens": DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS,
                    "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE["planner"],
                }
            },
            "planner",
            prompt,
        )
    except ClaudeContextBudgetError as exc:
        assert expected_available_output_tokens < MIN_DYNAMIC_MAX_OUTPUT_TOKENS
        assert exc.available_output_tokens == expected_available_output_tokens
        assert exc.estimated_input_tokens == estimated_input_tokens
    else:
        raise AssertionError("Expected ClaudeContextBudgetError")


def test_claude_adapter_fails_preflight_without_invoking_runner_for_oversized_prompt(tmp_path: Path) -> None:
    runner = FakeRunner()
    user_prompt = _ascii_prompt_for_estimated_tokens(DEFAULT_CONTEXT_WINDOW_TOKENS)

    result = ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(
            tmp_path,
            config={
                "claude": {
                    "context_window_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
                    "context_window_buffer_tokens": DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS,
                    "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE["planner"],
                }
            },
            user_prompt=user_prompt,
        )
    )

    assert result.status == "FAILED"
    assert runner.command is None
    diagnostics = tmp_path / "workspace" / "logs" / "request_diagnostics.md"
    assert diagnostics.exists()
    assert "preflight_context_budget_error: `true`" in diagnostics.read_text(encoding="utf-8")


def test_codex_adapter_applies_context_window_and_max_output_config(tmp_path: Path) -> None:
    runner = FakeRunner()

    CodexCLIAdapter(command=["codex", "exec"], runner=runner).run(_context(tmp_path, config={}))

    assert runner.command is not None
    assert "-c" in runner.command
    assert "model_context_window=199999" in runner.command
    assert "max_output_tokens=64000" in runner.command
    assert runner.env is None


def test_codex_adapter_uses_runtime_paths_in_docker_mode(tmp_path: Path) -> None:
    runner = RuntimeAwareFakeRunner()

    CodexCLIAdapter(command=["codex", "exec"], runner=runner).run(_docker_context(tmp_path, config={}))

    assert runner.command is not None
    assert runner.runtime_spec is not None and runner.runtime_spec.is_docker
    assert runner.command[runner.command.index("--cd") + 1] == "/workspace"
    add_dirs = [runner.command[index + 1] for index, value in enumerate(runner.command) if value == "--add-dir"]
    assert add_dirs == ["/openorchestra/input", "/openorchestra/output"]


def test_codex_adapter_lowers_max_output_for_large_prompt(tmp_path: Path) -> None:
    runner = FakeRunner()
    user_prompt = _ascii_prompt_for_estimated_tokens(65_000)

    CodexCLIAdapter(command=["codex", "exec"], runner=runner).run(
        _context(
            tmp_path,
            config={
                "claude": {
                    "context_window_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
                    "context_window_buffer_tokens": DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS,
                    "max_output_tokens": {"planner": 128_000},
                }
            },
            user_prompt=user_prompt,
        )
    )

    assert runner.command is not None
    assert runner.input_text is not None
    max_output_arg = next(arg for arg in runner.command if arg.startswith("max_output_tokens="))
    adjusted_max_output_tokens = int(max_output_arg.split("=", 1)[1])
    expected_available_output_tokens = _available_output_tokens(runner.input_text)
    assert adjusted_max_output_tokens == expected_available_output_tokens
    assert adjusted_max_output_tokens < 128_000


def test_codex_adapter_fails_preflight_without_invoking_runner_for_oversized_prompt(tmp_path: Path) -> None:
    runner = FakeRunner()

    result = CodexCLIAdapter(command=["codex", "exec"], runner=runner).run(
        _context(
            tmp_path,
            config={
                "claude": {
                    "context_window_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
                    "context_window_buffer_tokens": DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS,
                    "max_output_tokens": {"planner": 128_000},
                }
            },
            user_prompt=_ascii_prompt_for_estimated_tokens(DEFAULT_CONTEXT_WINDOW_TOKENS),
        )
    )

    assert result.status == "FAILED"
    assert runner.command is None
    diagnostics = tmp_path / "workspace" / "logs" / "request_diagnostics.md"
    assert diagnostics.exists()
    diagnostics_text = diagnostics.read_text(encoding="utf-8")
    assert "- backend: `codex`" in diagnostics_text
    assert "preflight_context_budget_error: `true`" in diagnostics_text


def test_gemini_adapter_uses_headless_prompt_and_includes_workspace_dirs(tmp_path: Path) -> None:
    runner = FakeRunner()

    HeadlessCLIAdapter("gemini", runner=runner).run(_context(tmp_path, config={}))

    assert runner.command is not None
    assert runner.command[:3] == ["gemini", "--prompt", ""]
    assert "--output-format" in runner.command
    assert "--skip-trust" in runner.command
    assert "--include-directories" in runner.command
    assert "--approval-mode" in runner.command
    assert "yolo" in runner.command
    assert runner.input_text is not None


def test_qwen_adapter_uses_headless_prompt_and_configured_approval_mode(tmp_path: Path) -> None:
    runner = FakeRunner()

    HeadlessCLIAdapter("qwen", runner=runner).run(
        _context(
            tmp_path,
            config={
                "qwen": {
                    "approval_mode": "auto-edit",
                    "auth_type": "openai",
                    "openai_base_url": "https://api.example.test/v1",
                }
            },
        )
    )

    assert runner.command is not None
    assert runner.command[:3] == ["qwen", "--prompt", ""]
    assert "--bare" in runner.command
    assert "--auth-type" in runner.command
    assert "openai" in runner.command
    assert "--openai-base-url" in runner.command
    assert "https://api.example.test/v1" in runner.command
    assert "--approval-mode" in runner.command
    assert "auto-edit" in runner.command
    assert runner.input_text is not None


def test_headless_adapter_normalizes_approval_mode_for_cli_flavors(tmp_path: Path) -> None:
    gemini_runner = FakeRunner()
    qwen_runner = FakeRunner()

    HeadlessCLIAdapter("gemini", runner=gemini_runner).run(
        _context(tmp_path / "gemini", config={"gemini": {"approval_mode": "auto-edit"}})
    )
    HeadlessCLIAdapter("qwen", runner=qwen_runner).run(
        _context(tmp_path / "qwen", config={"qwen": {"approval_mode": "auto_edit"}})
    )

    assert gemini_runner.command is not None
    assert "auto_edit" in gemini_runner.command
    assert qwen_runner.command is not None
    assert "auto-edit" in qwen_runner.command


def test_headless_adapter_uses_runtime_paths_in_docker_mode(tmp_path: Path) -> None:
    runner = RuntimeAwareFakeRunner()

    HeadlessCLIAdapter("gemini", runner=runner).run(_docker_context(tmp_path, config={}))

    assert runner.command is not None
    assert runner.runtime_spec is not None and runner.runtime_spec.is_docker
    include_dirs = [
        runner.command[index + 1]
        for index, value in enumerate(runner.command)
        if value == "--include-directories"
    ]
    assert include_dirs == ["/openorchestra/input", "/openorchestra/output"]


def test_claude_adapter_uses_role_specific_max_output_tokens(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(
            tmp_path,
            role="executor",
            config={"claude": {"max_output_tokens": {"planner": 24000, "executor": 48000, "reviewer": 12000}}},
        )
    )

    assert runner.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "48000"}
    assert runner.command is not None
    settings_path = Path(runner.command[runner.command.index("--settings") + 1])
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "48000"}
    }


def test_claude_adapter_uses_role_specific_default_when_mapping_omits_role(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(tmp_path, role="communicator", config={"claude": {"max_output_tokens": {"reviewer": 12000}}})
    )

    assert runner.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}


def test_claude_adapter_can_disable_max_output_override(tmp_path: Path) -> None:
    runner = FakeRunner()

    ClaudeCodeAdapter(command=["claude", "-p"], runner=runner).run(
        _context(tmp_path, config={"claude": {"max_output_tokens": 0}})
    )

    assert runner.env == {}
    assert runner.command is not None
    assert "--settings" not in runner.command


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
