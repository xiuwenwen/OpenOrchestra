from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import uuid
from typing import Any, Protocol

from harness.adapters.claude_config import claude_env_for_role, write_claude_invocation_settings
from harness.adapters.headless_cli_adapter import SUPPORTED_HEADLESS_CLI_BACKENDS, headless_cli_command
from harness.adapters.runner_invocation import run_subprocess_runner
from harness.adapters.subprocess_runner import SubprocessRunner
from harness.agents.context import AgentRunContext
from harness.artifacts.validator import ValidationResult


DELIVERY_CONTRACT_REVIEW_PROMPT_VERSION = "delivery-contract-review/v1"
DELIVERY_CONTRACT_REVIEW_JSON_SCHEMA = {
    "decision": "accept|retry",
    "delivery_return_code": "integer|null",
    "instruction_following_issue": "boolean",
    "actual_role_success": "boolean",
    "reason": "short string",
}


class Runner(Protocol):
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
        ...


@dataclass(frozen=True)
class DeliveryContractReview:
    decision: str
    delivery_return_code: int | None
    instruction_following_issue: bool
    actual_role_success: bool
    reason: str
    prompt_path: Path
    stdout_path: Path | None = None
    stderr_path: Path | None = None

    @property
    def accepts_format_only_failure(self) -> bool:
        # Accept only the narrow case where the role succeeded but delivery.md
        # failed the Harness return-envelope format contract.
        return self.decision == "accept" and self.actual_role_success and self.instruction_following_issue

    @property
    def accepts(self) -> bool:
        return self.accepts_format_only_failure


class DeliveryContractReviewer:
    def __init__(self, runner: Runner | None = None):
        self.runner = runner or SubprocessRunner()

    def review(
        self,
        *,
        backend: str,
        context: AgentRunContext,
        validation_result: ValidationResult,
    ) -> DeliveryContractReview:
        run_dir = context.log_dir / "delivery_contract_review" / str(uuid.uuid4())
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt = self.build_prompt(context, validation_result)
        prompt_path = run_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        if backend == "mock":
            return self._mock_review(context, validation_result, prompt_path)

        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        env = claude_env_for_role(context.config, "reviewer", prompt) if backend == "claude" else None
        settings_path = write_claude_invocation_settings(run_dir, env or {}) if backend == "claude" else None
        runtime_settings_path = context.runtime_path(settings_path) if settings_path else None
        runtime_run_dir = context.runtime_path(run_dir)
        command = self._command(backend, run_dir, runtime_run_dir, runtime_settings_path)
        (run_dir / "command.txt").write_text(" ".join(command), encoding="utf-8")
        if env:
            (run_dir / "env_overrides.txt").write_text(
                "\n".join(f"{key}={value}" for key, value in sorted(env.items())) + "\n",
                encoding="utf-8",
            )
        exit_code = run_subprocess_runner(
            self.runner,
            command,
            run_dir,
            context.timeout_seconds,
            stdout_path,
            stderr_path,
            input_text=prompt,
            env=env,
            runtime_spec=context.runtime_spec,
        )
        if exit_code != 0:
            return DeliveryContractReview(
                decision="retry",
                delivery_return_code=None,
                instruction_following_issue=False,
                actual_role_success=False,
                reason=f"delivery contract review failed with exit_code={exit_code}",
                prompt_path=prompt_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        try:
            raw_output = stdout_path.read_text(encoding="utf-8", errors="replace")
            return self._parse_review(raw_output, prompt_path, stdout_path, stderr_path)
        except (json.JSONDecodeError, ValueError) as exc:
            return DeliveryContractReview(
                decision="retry",
                delivery_return_code=None,
                instruction_following_issue=False,
                actual_role_success=False,
                reason=f"delivery contract review returned invalid JSON: {exc}",
                prompt_path=prompt_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )

    def build_prompt(self, context: AgentRunContext, validation_result: ValidationResult) -> str:
        delivery_path = context.output_dir / "delivery.md"
        delivery_content = delivery_path.read_text(encoding="utf-8", errors="replace") if delivery_path.exists() else ""
        failed_prompt_path = context.log_dir / "prompt.md"
        failed_prompt = (
            failed_prompt_path.read_text(encoding="utf-8", errors="replace")
            if failed_prompt_path.exists()
            else ""
        )
        required_file_status = {
            name: {
                "exists": (context.output_dir / name).exists(),
                "is_file": (context.output_dir / name).is_file(),
                "size_bytes": (context.output_dir / name).stat().st_size if (context.output_dir / name).exists() else 0,
            }
            for name in context.required_outputs
        }
        issue_payload = [
            {
                "artifact": issue.artifact,
                "code": issue.code,
                "message": issue.message,
                "severity": issue.severity,
            }
            for issue in validation_result.issues
            if issue.severity == "error"
        ]
        metadata = {
            "prompt_version": DELIVERY_CONTRACT_REVIEW_PROMPT_VERSION,
            "task_id": context.task_id,
            "phase": context.phase,
            "role": context.role,
            "agent_id": context.agent_id,
            "round_id": context.round_id,
            "required_outputs": context.required_outputs,
            "required_file_status": required_file_status,
            "validation_errors": issue_payload,
        }
        return "\n".join(
            [
                "# Delivery Contract Review",
                "",
                "You are a Harness contract reviewer. Decide whether a role run that failed only the delivery.md "
                "return-envelope contract should be accepted or retried.",
                "",
                "Return exactly one JSON object and no Markdown/prose/code fence.",
                f"Required JSON schema: {json.dumps(DELIVERY_CONTRACT_REVIEW_JSON_SCHEMA, ensure_ascii=False)}",
                "",
                "Decision rules:",
                "- Use `accept` only when the required non-delivery output files exist, are non-empty, and the "
                "provided delivery.md failure is only an instruction-following/format issue.",
                "- Use `retry` when any required non-delivery output is missing or empty, when delivery.md says the "
                "role truly failed, or when the task prompt required work that the output clearly did not complete.",
                "- A negative business result such as failed tests, blocking bugs, or changes required "
                "is not by itself a role delivery failure.",
                "- If you choose `accept`, set `delivery_return_code` to 0, `instruction_following_issue` to true, "
                "and `actual_role_success` to true.",
                "",
                "## Run Metadata",
                "```json",
                json.dumps(metadata, ensure_ascii=False, indent=2),
                "```",
                "",
                "## Failed Agent Task Prompt",
                failed_prompt,
                "",
                "## Submitted delivery.md",
                delivery_content,
                "",
            ]
        )

    def normalized_delivery_json(self, context: AgentRunContext, review: DeliveryContractReview) -> str:
        payload = {
            "return_code": 0,
            "task_status": "success",
            "role_return_code": 0,
            "role": context.role,
            "phase": context.phase,
            "agent_id": context.agent_id,
            "produced_files": list(context.required_outputs),
            "known_risks": ["delivery.md normalized by Harness after delivery-contract review"],
            "contract_review": {
                "prompt_version": DELIVERY_CONTRACT_REVIEW_PROMPT_VERSION,
                "decision": review.decision,
                "reason": review.reason,
                "prompt_path": str(review.prompt_path),
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def _command(
        self,
        backend: str,
        run_dir: Path,
        runtime_run_dir: str,
        settings_path: str | None = None,
    ) -> list[str]:
        if backend == "claude":
            command = ["claude", "-p"]
            if settings_path:
                command.extend(["--settings", settings_path])
            command.extend(["--output-format", "text"])
            return command
        if backend == "codex":
            return ["codex", "exec", "--skip-git-repo-check", "--cd", runtime_run_dir or str(run_dir), "-"]
        if backend in SUPPORTED_HEADLESS_CLI_BACKENDS:
            return headless_cli_command(backend)
        return ["codex", "exec", "--skip-git-repo-check", "--cd", runtime_run_dir or str(run_dir), "-"]

    def _parse_review(
        self,
        raw_output: str,
        prompt_path: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> DeliveryContractReview:
        payload = self._extract_json_object(raw_output)
        decision = str(payload.get("decision", "")).strip().lower()
        if decision not in {"accept", "retry"}:
            raise ValueError("decision must be accept or retry")
        return_code = payload.get("delivery_return_code")
        if isinstance(return_code, bool):
            return_code = None
        if isinstance(return_code, str) and return_code.strip():
            return_code = int(return_code.strip())
        if not isinstance(return_code, int):
            return_code = None
        return DeliveryContractReview(
            decision=decision,
            delivery_return_code=return_code,
            instruction_following_issue=bool(payload.get("instruction_following_issue")),
            actual_role_success=bool(payload.get("actual_role_success")),
            reason=str(payload.get("reason", "")).strip()[:500],
            prompt_path=prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    def _extract_json_object(self, raw_output: str) -> dict[str, Any]:
        text = raw_output.strip()
        if not text:
            raise ValueError("empty output")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if fenced:
                payload = json.loads(fenced.group(1))
            else:
                object_match = re.search(r"\{.*\}", text, re.DOTALL)
                if not object_match:
                    raise
                payload = json.loads(object_match.group(0))
        if not isinstance(payload, dict):
            raise ValueError("review output must be a JSON object")
        return payload

    def _mock_review(
        self,
        context: AgentRunContext,
        validation_result: ValidationResult,
        prompt_path: Path,
    ) -> DeliveryContractReview:
        non_delivery_ok = all(
            (context.output_dir / name).exists()
            and (context.output_dir / name).is_file()
            and (context.output_dir / name).stat().st_size > 0
            for name in context.required_outputs
            if name != "delivery.md"
        )
        delivery_path = context.output_dir / "delivery.md"
        delivery_text = (
            delivery_path.read_text(encoding="utf-8", errors="replace").lower() if delivery_path.exists() else ""
        )
        looks_successful = any(
            token in delivery_text for token in ("success", "complete", "completed", "return_code: 0")
        )
        delivery_only = all(
            issue.artifact == "delivery.md" for issue in validation_result.issues if issue.severity == "error"
        )
        accept = non_delivery_ok and delivery_only and looks_successful
        return DeliveryContractReview(
            decision="accept" if accept else "retry",
            delivery_return_code=0 if accept else None,
            instruction_following_issue=accept,
            actual_role_success=accept,
            reason="mock review accepted delivery format-only failure" if accept else "mock review requires retry",
            prompt_path=prompt_path,
        )
