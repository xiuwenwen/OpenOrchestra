from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Protocol

from harness.adapters.claude_config import claude_env_for_role, write_claude_invocation_settings
from harness.adapters.headless_cli_adapter import SUPPORTED_HEADLESS_CLI_BACKENDS, headless_cli_command
from harness.adapters.subprocess_runner import SubprocessRunner
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, MISC, NEW_PROJECT, WORKFLOW_TYPES, normalize_workflow_type


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


class WorkflowClassificationError(RuntimeError):
    pass


class WorkflowClassifier:
    def __init__(
        self,
        backend: str,
        runner: Runner | None = None,
        log_root: Path | str = "logs/workflow_classifier",
        config: dict[str, Any] | None = None,
    ):
        self.backend = backend
        self.runner = runner or SubprocessRunner()
        self.log_root = Path(log_root)
        self.config = config or {}

    def classify(self, user_prompt: str, timeout_seconds: int = 0) -> tuple[str, Path]:
        workflow_type, run_dir, _ = self.classify_with_fallback(user_prompt, timeout_seconds)
        return workflow_type, run_dir

    def classify_with_fallback(self, user_prompt: str, timeout_seconds: int = 0) -> tuple[str, Path, str | None]:
        run_dir = self.log_root / str(uuid.uuid4())
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt = self._classification_prompt(user_prompt)
        (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        env = claude_env_for_role(self.config, "classifier", prompt) if self.backend == "claude" else None
        settings_path = write_claude_invocation_settings(run_dir, env or {}) if self.backend == "claude" else None
        command = self._command(run_dir, settings_path)
        (run_dir / "command.txt").write_text(" ".join(command), encoding="utf-8")
        if env:
            (run_dir / "env_overrides.txt").write_text(
                "\n".join(f"{key}={value}" for key, value in sorted(env.items())) + "\n",
                encoding="utf-8",
            )
        exit_code = self.runner.run(command, run_dir, timeout_seconds, stdout_path, stderr_path, input_text=prompt, env=env)
        if exit_code != 0:
            raise WorkflowClassificationError(
                f"Workflow classifier failed with exit_code={exit_code}. See logs: {run_dir}"
            )
        raw_output = stdout_path.read_text(encoding="utf-8", errors="replace")
        try:
            workflow_type = self._parse_workflow_type(raw_output)
        except WorkflowClassificationError as exc:
            if "did not contain JSON" in str(exc) and raw_output.strip() and self._looks_like_safety_refusal(raw_output):
                return MISC, run_dir, raw_output.strip()
            if "did not contain JSON" in str(exc) and raw_output.strip() and self._can_use_raw_misc_answer(user_prompt):
                return MISC, run_dir, raw_output.strip()
            raise
        return workflow_type, run_dir, None

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
        raise WorkflowClassificationError(f"Unsupported workflow classifier backend: {self.backend}")

    def _parse_workflow_type(self, raw_output: str) -> str:
        payload = self._extract_json(raw_output)
        if not isinstance(payload, dict):
            raise WorkflowClassificationError("Workflow classifier did not return a JSON object.")
        value = str(payload.get("workflow_type", "")).strip()
        try:
            return normalize_workflow_type(value)
        except ValueError as exc:
            allowed = ", ".join(sorted(WORKFLOW_TYPES))
            raise WorkflowClassificationError(
                f"Workflow classifier returned unsupported workflow_type={value!r}. Allowed values: {allowed}."
            ) from exc

    def _extract_json(self, raw_output: str) -> dict[str, object]:
        text = raw_output.strip()
        if not text:
            raise WorkflowClassificationError("Workflow classifier returned empty output.")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            return json.loads(fenced.group(1))
        object_match = re.search(r"\{.*\}", text, re.DOTALL)
        if object_match:
            return json.loads(object_match.group(0))
        raise WorkflowClassificationError("Workflow classifier output did not contain JSON.")

    def _can_use_raw_misc_answer(self, user_prompt: str) -> bool:
        """Allow raw-answer fallback only for clearly non-project requests.

        Some real CLIs answer conversational questions directly instead of obeying
        the JSON-only classifier prompt. That is acceptable for misc questions,
        but for project-changing prompts it would silently skip the Harness flow.
        """
        text = user_prompt.strip().lower()
        project_markers = (
            "build",
            "create",
            "implement",
            "add",
            "modify",
            "change",
            "fix",
            "repair",
            "debug",
            "develop",
            "write a",
            "make a",
            "new project",
            "feature",
            "bug",
            "实现",
            "创建",
            "新建",
            "新增",
            "增加",
            "修改",
            "修复",
            "调试",
            "开发",
            "写一个",
            "做一个",
            "加一个",
            "软件",
            "程序",
            "功能",
            "工程",
            "项目",
        )
        return not any(marker in text for marker in project_markers)

    def _looks_like_safety_refusal(self, raw_output: str) -> bool:
        text = raw_output.lower()
        refusal_markers = (
            "i can't help with this request",
            "i cannot help with this request",
            "i can't assist with",
            "i cannot assist with",
            "can't help create",
            "cannot help create",
            "enable sms bombing",
            "sms bombing",
            "abuse infrastructure",
            "violate terms of service",
            "computer misuse",
            "unauthorized automation",
        )
        return any(marker in text for marker in refusal_markers)

    def _classification_prompt(self, user_prompt: str) -> str:
        return "\n".join(
            [
                "# Harness Workflow Classification",
                "",
                "Classify the user's request into exactly one Harness workflow type.",
                "",
                "Allowed workflow_type values:",
                f"- `{BUGFIX}`: The user asks to repair a defect, failure, regression, broken behavior, error, crash, test failure, or incorrect existing behavior.",
                f"- `{FEATURE_CHANGE}`: The user asks to add, remove, extend, refactor, or modify behavior in an existing project.",
                f"- `{NEW_PROJECT}`: The user asks to create a new project, app, tool, script, or system from scratch.",
                f"- `{MISC}`: The user asks a question, requests an explanation, asks for analysis, asks how something works, or requests advice without asking to create or modify project files.",
                "",
                "Decision rules:",
                "- Prefer `bugfix` only when the request is primarily about fixing existing broken behavior.",
                "- Prefer `feature_change` when the request assumes an existing codebase and asks for new or changed capability.",
                "- Prefer `new_project` when the request asks to build something new and does not reference existing behavior.",
                "- Prefer `misc` when the request is informational, conversational, diagnostic, explanatory, or advisory and does not require changing or creating code artifacts.",
                "- If ambiguous between feature_change and new_project, choose new_project only when there is no clear existing project context.",
                "- If ambiguous between misc and a project workflow, choose misc unless the user explicitly asks to build, change, fix, or deliver files.",
                "",
                "Return exactly one JSON object and no prose:",
                '{"workflow_type":"bugfix|feature_change|new_project|misc","confidence":0.0,"reason":"short reason"}',
                "",
                "User request:",
                user_prompt,
            ]
        )
