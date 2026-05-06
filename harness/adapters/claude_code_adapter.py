from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from harness.adapters.base import AgentAdapter
from harness.adapters.claude_config import ClaudeContextBudgetError, claude_env_for_role
from harness.adapters.subprocess_runner import SubprocessRunner
from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult
from harness.prompts.builder import PromptBuilder


REQUEST_SIZE_ERROR_PATTERNS = (
    "ContextWindowExceededError",
    "maximum context length",
    "parameter=input_tokens",
    "Please reduce the length of the input prompt",
    "Request too large",
    "request_too_large",
    "budget_exceeds_model_limit",
    "max 32MB",
    "too many tokens",
    "exceeds model limit",
)


class ClaudeCodeAdapter(AgentAdapter):
    def __init__(self, command: list[str] | None = None, runner: SubprocessRunner | None = None):
        self.command = command or ["claude", "-p"]
        self.runner = runner or SubprocessRunner(stream_output=True, stream_prefix="[claude] ")
        self.prompt_builder = PromptBuilder()

    def run(self, context: AgentRunContext) -> AgentRunResult:
        prompt = self.prompt_builder.build(context)
        context.log_dir.mkdir(parents=True, exist_ok=True)
        (context.log_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        stdout_path = context.log_dir / "stdout.log"
        stderr_path = context.log_dir / "stderr.log"
        command = self.command + self._extra_args(context)
        redacted_command = list(command)
        (context.log_dir / "command.txt").write_text(" ".join(redacted_command), encoding="utf-8")
        try:
            env_overrides = self._env(context, prompt)
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
        (context.log_dir / "env_overrides.txt").write_text(
            "\n".join(f"{key}={value}" for key, value in sorted(env_overrides.items())) + "\n",
            encoding="utf-8",
        )
        exit_code = self.runner.run(
            command,
            context.repo_dir,
            context.timeout_seconds,
            stdout_path,
            stderr_path,
            input_text=prompt,
            env=env_overrides,
        )
        diagnostics_path = self._maybe_write_request_diagnostics(
            context=context,
            prompt=prompt,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            exit_code=exit_code,
            env_overrides=env_overrides,
        )
        if diagnostics_path:
            with stderr_path.open("a", encoding="utf-8") as stderr:
                stderr.write(f"\nHarness request diagnostics: {diagnostics_path}\n")
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

    def _extra_args(self, context: AgentRunContext) -> list[str]:
        args: list[str] = []
        permission_mode = context.config.get("claude", {}).get("permission_mode")
        if permission_mode:
            args.extend(["--permission-mode", str(permission_mode)])
        args.extend(
            [
                "--output-format",
                "text",
                "--add-dir",
                str(context.input_dir),
                str(context.output_dir),
            ]
        )
        return args

    def _env(self, context: AgentRunContext, prompt: str) -> dict[str, str]:
        return claude_env_for_role(context.config, context.role, prompt)

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
            "- exit_code: `not invoked`",
            f"- prompt_bytes: `{self._byte_len(prompt)}`",
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

    def _maybe_write_request_diagnostics(
        self,
        *,
        context: AgentRunContext,
        prompt: str,
        stdout_path: Path,
        stderr_path: Path,
        exit_code: int,
        env_overrides: dict[str, str],
    ) -> Path | None:
        stdout_text = self._read_text(stdout_path)
        stderr_text = self._read_text(stderr_path)
        missing_outputs = [name for name in context.required_outputs if not (context.output_dir / name).exists()]
        combined_logs = f"{stdout_text}\n{stderr_text}"
        has_size_error = any(pattern in combined_logs for pattern in REQUEST_SIZE_ERROR_PATTERNS)

        if exit_code == 0 and not has_size_error and not missing_outputs:
            return None

        diagnostics_path = context.log_dir / "request_diagnostics.md"
        claude_jsonl = self._latest_claude_jsonl(context.repo_dir)
        jsonl_summary = self._summarize_claude_jsonl(claude_jsonl) if claude_jsonl else {}
        output_sizes = self._directory_file_sizes(context.output_dir)
        input_sizes = self._directory_file_sizes(context.input_dir)

        lines = [
            "# Request Diagnostics",
            "",
            "## Run",
            f"- task_id: `{context.task_id}`",
            f"- phase: `{context.phase}`",
            f"- role: `{context.role}`",
            f"- agent_id: `{context.agent_id}`",
            f"- exit_code: `{exit_code}`",
            f"- prompt_bytes: `{self._byte_len(prompt)}`",
            f"- stdout_bytes: `{stdout_path.stat().st_size if stdout_path.exists() else 0}`",
            f"- stderr_bytes: `{stderr_path.stat().st_size if stderr_path.exists() else 0}`",
            f"- max_output_tokens_env: `{env_overrides.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS', 'unset')}`",
            "",
            "## Error Signals",
            f"- request_size_error_detected: `{str(has_size_error).lower()}`",
            f"- missing_required_outputs: `{', '.join(missing_outputs) if missing_outputs else 'none'}`",
        ]
        matching_lines = self._matching_log_lines(combined_logs)
        if matching_lines:
            lines.extend(["- matching_log_lines:"] + [f"  - `{line}`" for line in matching_lines])

        lines.extend(
            [
                "",
                "## Local File Distribution",
                f"- input_dir_bytes: `{self._directory_size(context.input_dir)}`",
                f"- output_dir_bytes: `{self._directory_size(context.output_dir)}`",
                "- largest_input_files:",
                *self._format_size_rows(input_sizes[:10]),
                "- largest_output_files:",
                *self._format_size_rows(output_sizes[:10]),
                "",
                "## Claude JSONL",
                f"- jsonl_path: `{claude_jsonl or 'not found'}`",
            ]
        )
        if jsonl_summary:
            lines.extend(self._format_jsonl_summary(jsonl_summary))
        else:
            lines.append("- summary: `not available`")

        diagnostics_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return diagnostics_path

    def _summarize_claude_jsonl(self, jsonl_path: Path) -> dict[str, Any]:
        category_bytes: Counter[str] = Counter()
        tool_input_bytes: Counter[str] = Counter()
        largest_payloads: list[tuple[int, str, str]] = []
        error_lines: list[str] = []
        max_input_tokens = 0
        max_output_tokens = 0
        last_usage: dict[str, Any] = {}
        records = 0

        with jsonl_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                records += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                text = json.dumps(record, ensure_ascii=False)
                if any(pattern in text for pattern in REQUEST_SIZE_ERROR_PATTERNS):
                    error_lines.append(self._trim(text, 240))

                usage = self._extract_usage(record)
                if usage:
                    max_input_tokens = max(max_input_tokens, int(usage.get("input_tokens") or 0))
                    max_output_tokens = max(max_output_tokens, int(usage.get("output_tokens") or 0))
                    last_usage = usage

                if "toolUseResult" in record:
                    category_bytes["top_level_toolUseResult"] += self._payload_size(record["toolUseResult"])
                    self._collect_file_payload(largest_payloads, "toolUseResult", record["toolUseResult"])

                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                for item in message.get("content") or []:
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("type") or "unknown")
                    if item_type == "tool_use":
                        tool_name = str(item.get("name") or "unknown")
                        size = self._payload_size(item.get("input"))
                        category_bytes[f"tool_use_input:{tool_name}"] += size
                        tool_input_bytes[tool_name] += size
                        self._collect_file_payload(largest_payloads, f"tool_use:{tool_name}", item.get("input"))
                    elif item_type == "tool_result":
                        category_bytes["message_tool_result_content"] += self._payload_size(item.get("content"))
                        self._collect_file_payload(largest_payloads, "tool_result", item.get("content"))
                    elif item_type == "text":
                        category_bytes["message_text"] += self._byte_len(str(item.get("text") or ""))

        largest_payloads.sort(reverse=True, key=lambda row: row[0])
        return {
            "jsonl_bytes": jsonl_path.stat().st_size,
            "records": records,
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "last_usage": last_usage,
            "category_bytes": category_bytes.most_common(12),
            "tool_input_bytes": tool_input_bytes.most_common(12),
            "largest_payloads": largest_payloads[:12],
            "error_lines": error_lines[:5],
        }

    def _format_jsonl_summary(self, summary: dict[str, Any]) -> list[str]:
        lines = [
            f"- jsonl_bytes: `{summary['jsonl_bytes']}`",
            f"- jsonl_records: `{summary['records']}`",
            f"- max_input_tokens: `{summary['max_input_tokens']}`",
            f"- max_output_tokens: `{summary['max_output_tokens']}`",
            f"- last_usage: `{json.dumps(summary['last_usage'], ensure_ascii=False)}`",
            "- category_bytes:",
        ]
        lines.extend(self._format_counter_rows(summary["category_bytes"]))
        lines.append("- tool_input_bytes:")
        lines.extend(self._format_counter_rows(summary["tool_input_bytes"]))
        lines.append("- largest_recorded_payloads:")
        if summary["largest_payloads"]:
            lines.extend(f"  - `{size}` bytes | `{label}` | `{path}`" for size, label, path in summary["largest_payloads"])
        else:
            lines.append("  - none")
        if summary["error_lines"]:
            lines.append("- jsonl_error_snippets:")
            lines.extend(f"  - `{line}`" for line in summary["error_lines"])
        return lines

    def _latest_claude_jsonl(self, repo_dir: Path) -> Path | None:
        project_slug = str(repo_dir.resolve()).replace("/", "-").replace("_", "-")
        project_dir = Path.home() / ".claude" / "projects" / project_slug
        if not project_dir.exists():
            return None
        jsonl_files = [path for path in project_dir.glob("*.jsonl") if path.is_file()]
        if not jsonl_files:
            return None
        return max(jsonl_files, key=lambda path: path.stat().st_mtime)

    def _extract_usage(self, record: dict[str, Any]) -> dict[str, Any]:
        message = record.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            return message["usage"]
        if isinstance(record.get("usage"), dict):
            return record["usage"]
        return {}

    def _collect_file_payload(self, rows: list[tuple[int, str, str]], label: str, payload: Any) -> None:
        if isinstance(payload, dict):
            path = payload.get("file_path") or payload.get("path") or payload.get("filename") or "unknown"
            content = payload.get("content") or payload.get("new_string") or payload.get("old_string")
            if isinstance(content, str):
                rows.append((self._byte_len(content), label, str(path)))
            for value in payload.values():
                self._collect_file_payload(rows, label, value)
        elif isinstance(payload, list):
            for value in payload:
                self._collect_file_payload(rows, label, value)

    def _matching_log_lines(self, text: str) -> list[str]:
        matches = []
        for line in text.splitlines():
            if any(pattern in line for pattern in REQUEST_SIZE_ERROR_PATTERNS):
                matches.append(self._trim(line, 240))
        return matches[:8]

    def _directory_file_sizes(self, directory: Path) -> list[tuple[int, Path]]:
        if not directory.exists():
            return []
        sizes = []
        for path in directory.rglob("*"):
            if path.is_file():
                try:
                    sizes.append((path.stat().st_size, path))
                except OSError:
                    continue
        return sorted(sizes, reverse=True, key=lambda row: row[0])

    def _directory_size(self, directory: Path) -> int:
        return sum(size for size, _ in self._directory_file_sizes(directory))

    def _format_size_rows(self, rows: list[tuple[int, Path]]) -> list[str]:
        if not rows:
            return ["  - none"]
        return [f"  - `{size}` bytes | `{path}`" for size, path in rows]

    def _format_counter_rows(self, rows: list[tuple[str, int]]) -> list[str]:
        if not rows:
            return ["  - none"]
        return [f"  - `{name}`: `{size}`" for name, size in rows]

    def _payload_size(self, payload: Any) -> int:
        if payload is None:
            return 0
        if isinstance(payload, str):
            return self._byte_len(payload)
        return self._byte_len(json.dumps(payload, ensure_ascii=False, default=str))

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def _byte_len(self, value: str) -> int:
        return len(value.encode("utf-8"))

    def _trim(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 3] + "..."
