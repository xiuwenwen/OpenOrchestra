from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
import re
from pathlib import Path
from typing import Any, Callable

from harness.adapters.base import AgentAdapter
from harness.adapters.claude_code_adapter import ClaudeCodeAdapter
from harness.adapters.codex_cli_adapter import CodexCLIAdapter
from harness.adapters.headless_cli_adapter import HeadlessCLIAdapter
from harness.adapters.mock_adapter import MockAgentAdapter
from harness.agents.result import AgentRunResult, ArtifactRef
from harness.agents.runner import AgentPhaseRunner
from harness.artifacts.hashing import sha256_file
from harness.artifacts.manager import ArtifactManager
from harness.artifacts.schemas import required_outputs_for
from harness.artifacts.validator import ArtifactValidator
from harness.artifacts.visibility import ARTIFACT_VISIBILITY_RULES, TEST_REPORT_ARTIFACTS, ArtifactVisibilityPolicy
from harness.communication.communicator import Communicator
from harness.config.loader import load_config
from harness.config.runtime import RuntimeConfigService
from harness.core.errors import TaskFailedError
from harness.core.progress import ProgressCallback, ProgressEvent
from harness.core.state_machine import (
    COMPLETED,
    CREATED,
    DELIVERY,
    EXECUTION,
    FAILED,
    FINAL_JUDGEMENT,
    FIXING,
    MISC_RESPONSE,
    PATCH_MERGE,
    PLAN_REVIEW,
    PLAN_JUDGEMENT,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEW_JUDGEMENT,
    REVIEWING,
    RUNNING,
    TEST_JUDGEMENT,
    TESTING,
)
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, MISC, NEW_PROJECT, normalize_workflow_type
from harness.judge.decision_parser import parse_decision_file
from harness.judge.judge_runner import MockJudge
from harness.logs.logger import get_logger
from harness.patch.gate import (
    PatchGatePolicy,
    materialized_repo_markdown,
    objective_gate_markdown,
    patch_validation_markdown,
    run_patch_gate,
)
from harness.prompts.builder import PromptBuilder
from harness.state.db import StateDB
from harness.state.repository import StateRepository
from harness.workflow.engine import WorkflowEngine
from harness.workspace.manager import WorkspaceManager


ROLE_INSTRUCTIONS = {
    "planner": (
        "Create planning artifacts only. Analyze the request, existing artifacts, assumptions, risks, "
        "compatibility constraints, and an actionable task breakdown. Do not modify source files. "
        "delivery.md is a role return envelope. It must contain `return_code: 0` "
        "when you produced the required planning files, even if you identify high risks. "
        "Complete planning Markdown artifacts must contain `artifact_result_code: 0`."
    ),
    "executor": (
        "Create the artifacts required by the current executor phase. For implementation and fix phases, "
        "express code changes as unified diff files and supporting notes. For miscellaneous response phases, "
        "answer the request without modifying project files. Do not decide workflow progression or communicate "
        "with the user outside required artifacts. delivery.md is a role return envelope. It must contain "
        "`return_code: 0` when you produced the required files, regardless of the "
        "implementation complexity. Complete executor Markdown artifacts must contain `artifact_result_code: 0`."
    ),
    "tester": (
        "Evaluate executor artifacts and available repository state. Produce build, test, and bug reports "
        "with an explicit pass/fail assessment and reproducible evidence. "
        "IMPORTANT: delivery.md is a role return envelope, not the test verdict. It must contain "
        "`return_code: 0` as long as you completed the evaluation and produced the required reports, "
        "even if the test verdict is `test_result_code: -1` or you find critical bugs. "
        "`artifact_result_code` must be `0` for complete tester reports; put build/test/bug outcomes only in "
        "`build_result_code`, `test_result_code`, and `bug_result_code`."
    ),
    "reviewer": (
        "Review executor and tester artifacts for correctness, scope control, regressions, maintainability, "
        "and missing validation. Produce review findings only. delivery.md is a role return envelope. It must "
        "contain `return_code: 0` if you completed the review, regardless of whether "
        "the review verdict is `review_decision_code: 0` or `review_decision_code: 1`. "
        "`review_report.md` must contain `artifact_result_code: 0` when complete."
    ),
    "judge": (
        "Make the phase decision from collected artifacts only. Produce a strict machine-readable decision "
        "and a concise rationale. Do not create implementation changes. delivery.md is a role return envelope, "
        "not the phase verdict. It must contain `return_code: 0` if you rendered a "
        "clear decision, even when `decision.json` contains `decision: fail` or `decision: changes_required`. "
        "`decision_summary.md` must contain `artifact_result_code: 0` when complete."
    ),
    "communicator": (
        "Create the final delivery artifact only. Summarize outcome, status, produced artifacts, residual "
        "risks, and next steps using the accepted artifact set. delivery.md is a role return envelope. It must "
        "contain `return_code: 0` if the final delivery documentation is complete. "
        "`final_delivery.md` and `usage_guide.md` must contain `artifact_result_code: 0` when complete."
    ),
}


MATERIALIZED_SUCCESS_MARKER = ".harness_materialized_success.json"
SINGLE_EXECUTOR_FIX_PHASES = {FIXING, REVIEW_FIXING}
FixRoundLimitCallback = Callable[[str, int], str]


class Orchestrator:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        repository: StateRepository | None = None,
        workspace_manager: WorkspaceManager | None = None,
        artifact_manager: ArtifactManager | None = None,
        progress_callback: ProgressCallback | None = None,
        fix_round_limit_callback: FixRoundLimitCallback | None = None,
    ):
        self.config = config or load_config()
        system = self.config["system"]
        self.repository = repository or StateRepository(StateDB(system["state_db"]))
        self.config_service = RuntimeConfigService(self.config, self.repository)
        self.workspace_manager = workspace_manager or WorkspaceManager(system["workspace_root"])
        self.artifact_manager = artifact_manager or ArtifactManager(system["artifact_root"], self.repository)
        self.artifact_visibility = ArtifactVisibilityPolicy()
        self.validator = ArtifactValidator()
        self.communicator = Communicator(self.repository)
        self.judge = MockJudge()
        self.prompt_builder = PromptBuilder()
        self.role_instructions = ROLE_INSTRUCTIONS
        self.agent_runner = AgentPhaseRunner(self)
        self.workflow_engine = WorkflowEngine(self)
        self.logger = get_logger(__name__)
        self._active_task_id: str | None = None
        self._active_workflow_type: str | None = None
        self._active_task_resume_status: str | None = None
        self.progress_callback = progress_callback
        self.fix_round_limit_callback = fix_round_limit_callback

    def create_task(self, user_prompt: str, workflow_type: str | None = None) -> str:
        task_id = self.repository.create_task(user_prompt, CREATED, workflow_type=workflow_type)
        self._emit(ProgressEvent("task_created", task_id=task_id, status=CREATED, message="Task created"))
        return task_id

    def attach_project_context(self, task_id: str, content: str) -> None:
        if content.strip():
            self.artifact_manager.create_text_artifact(task_id, "project_context.md", content)

    def run_task(self, task_id: str, workflow_type: str | None = None) -> Path:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        workflow_type = normalize_workflow_type(workflow_type or task.get("workflow_type") or NEW_PROJECT)

        if not task.get("workflow_type"):
            self.repository.set_task_workflow_type(task_id, workflow_type)

        user_prompt = self._workflow_prompt(task["user_prompt"], workflow_type)
        self._active_task_id = task_id
        self._active_workflow_type = workflow_type
        self._active_task_resume_status = str(task.get("status") or "")
        self.repository.update_task(task_id, status=RUNNING)
        self._emit(
            ProgressEvent(
                "task_started",
                task_id=task_id,
                status=RUNNING,
                message=f"Task started with workflow={workflow_type}",
                data={"workflow_type": workflow_type},
            )
        )
        try:
            final_path = self.workflow_engine.run(task_id, workflow_type, user_prompt)
            self.repository.update_task(task_id, status=COMPLETED, current_phase=COMPLETED, current_role=None)
            result_label = "Response" if workflow_type == MISC else "Final delivery"
            event_data = {"result_path": str(final_path), "result_type": "response" if workflow_type == MISC else "final_delivery"}
            if workflow_type != MISC:
                event_data["success_path"] = str(Path(final_path).parent)
            self._emit(
                ProgressEvent(
                    "task_completed",
                    task_id=task_id,
                    phase=COMPLETED,
                    status=COMPLETED,
                    message=f"{result_label}: {final_path}",
                    data=event_data,
                )
            )
            return final_path
        except Exception as exc:
            self.repository.update_task(task_id, status=FAILED)
            self._emit(ProgressEvent("task_failed", task_id=task_id, status=FAILED, message=str(exc)))
            raise
        finally:
            self._active_task_id = None
            self._active_workflow_type = None
            self._active_task_resume_status = None

    def _run_new_project_flow(self, task_id: str, user_prompt: str) -> Path:
        return self.workflow_engine.run_new_project_flow(task_id, user_prompt)

    def _run_bugfix_flow(self, task_id: str, user_prompt: str) -> Path:
        return self.workflow_engine.run_bugfix_flow(task_id, user_prompt)

    def _bugfix_resume_start_round(self, task_id: str) -> int:
        return self.workflow_engine.bugfix_resume_start_round(task_id)

    def _highest_bugfix_round_id(self, task_id: str) -> int | None:
        return self.workflow_engine.highest_bugfix_round_id(task_id)

    def _run_feature_change_flow(self, task_id: str, user_prompt: str) -> Path:
        return self.workflow_engine.run_feature_change_flow(task_id, user_prompt)

    def _run_misc_flow(self, task_id: str, user_prompt: str) -> Path:
        return self.workflow_engine.run_misc_flow(task_id, user_prompt)

    def _run_planning_block(self, task_id: str, user_prompt: str) -> None:
        self.workflow_engine.run_planning_block(task_id, user_prompt)

    def _planning_peer_review_loop_count(self) -> int:
        return self.workflow_engine.planning_peer_review_loop_count()

    def _peer_reviews_satisfied(self, results: list[AgentRunResult]) -> bool:
        return self.workflow_engine.peer_reviews_satisfied(results)

    def _plan_review_approved(self, results: list[AgentRunResult]) -> bool:
        return self.workflow_engine.plan_review_approved(results)

    def _run_execution_test_loop(self, task_id: str, user_prompt: str) -> None:
        self.workflow_engine.run_execution_test_loop(task_id, user_prompt)

    def _execution_test_end_round(self, start_round: int, max_rounds: int | None) -> int | None:
        return self.workflow_engine.execution_test_end_round(start_round, max_rounds)

    def _execution_resume_start_round(self, task_id: str) -> int:
        return self.workflow_engine.execution_resume_start_round(task_id)

    def _max_test_fix_rounds(self) -> int | None:
        return self.workflow_engine.max_test_fix_rounds()

    def _resolve_test_fix_round_limit(self, task_id: str, current_limit: int | None) -> int | None:
        return self.workflow_engine.resolve_test_fix_round_limit(task_id, current_limit)

    def _highest_execution_test_round_id(self, task_id: str) -> int | None:
        return self.workflow_engine.highest_execution_test_round_id(task_id)

    def _run_review_loop(self, task_id: str, user_prompt: str) -> None:
        self.workflow_engine.run_review_loop(task_id, user_prompt)

    def _run_regression_test_fix_loop(self, task_id: str, user_prompt: str, review_round_id: int, merge_ok: bool) -> None:
        self.workflow_engine.run_regression_test_fix_loop(task_id, user_prompt, review_round_id, merge_ok)

    def _regression_phase_round_id(self, review_round_id: int, test_round_id: int, max_rounds: int | None) -> int:
        return self.workflow_engine.regression_phase_round_id(review_round_id, test_round_id, max_rounds)

    def _run_final_judgement(self, task_id: str, user_prompt: str) -> None:
        self.workflow_engine.run_final_judgement(task_id, user_prompt)

    def _run_delivery(self, task_id: str, user_prompt: str) -> Path:
        return self.workflow_engine.run_delivery(task_id, user_prompt)

    def run_role_phase(
        self,
        role: str,
        phase: str,
        round_id: int,
        required_outputs: list[str],
        user_prompt: str | None = None,
        agent_count_override: int | None = None,
    ) -> list[AgentRunResult]:
        return self.agent_runner.run_role_phase(
            role,
            phase,
            round_id,
            required_outputs,
            user_prompt,
            agent_count_override,
        )

    def _recover_phase_results(self, task_id: str, phase_id: str) -> list[AgentRunResult]:
        return self.agent_runner.recover_phase_results(task_id, phase_id)

    def _recovered_results_have_required_outputs(self, results: list[AgentRunResult], required_outputs: list[str]) -> bool:
        return self.agent_runner.recovered_results_have_required_outputs(results, required_outputs)

    def _run_agents_concurrently(
        self,
        adapter: AgentAdapter,
        task_id: str,
        phase_id: str,
        phase: str,
        role: str,
        agent_ids: list[str],
        round_id: int,
        user_prompt: str,
        required_outputs: list[str],
        timeout_seconds: int,
    ) -> list[AgentRunResult]:
        return self.agent_runner.run_agents_concurrently(
            adapter,
            task_id,
            phase_id,
            phase,
            role,
            agent_ids,
            round_id,
            user_prompt,
            required_outputs,
            timeout_seconds,
        )

    def _run_agent_with_retry(
        self,
        adapter: AgentAdapter,
        task_id: str,
        phase_id: str,
        phase: str,
        role: str,
        agent_id: str,
        round_id: int,
        user_prompt: str,
        required_outputs: list[str],
        timeout_seconds: int,
        cancel_event: Any | None = None,
    ) -> AgentRunResult:
        return self.agent_runner.run_agent_with_retry(
            adapter,
            task_id,
            phase_id,
            phase,
            role,
            agent_id,
            round_id,
            user_prompt,
            required_outputs,
            timeout_seconds,
            cancel_event,
        )

    def _is_request_size_failure(self, result: AgentRunResult, context: AgentRunContext, message: str) -> bool:
        return self.agent_runner.is_request_size_failure(result, context, message)

    def _logs_contain_request_size_error(self, log_dir: Path) -> bool:
        return self.agent_runner.logs_contain_request_size_error(log_dir)

    def _text_contains_request_size_error(self, text: str) -> bool:
        return self.agent_runner.text_contains_request_size_error(text)

    def _request_size_failure_message(self, context: AgentRunContext) -> str:
        return self.agent_runner.request_size_failure_message(context)

    def _run_adapter_with_heartbeat(self, adapter: AgentAdapter, context: AgentRunContext, attempt: int) -> AgentRunResult:
        return self.agent_runner.run_adapter_with_heartbeat(adapter, context, attempt)

    def _run_judge_phase(self, task_id: str, phase: str, round_id: int, user_prompt: str) -> dict[str, Any]:
        results = self.run_role_phase("judge", phase, round_id, required_outputs_for("judge", phase), user_prompt)
        phase_id = results[-1].phase_id

        # Check if decision already recorded in state store
        decisions = self.repository.list_judge_decisions(task_id)
        existing = next((d for d in decisions if d["phase_id"] == phase_id and d["decision_type"] == phase), None)
        if existing:
            return json.loads(existing["decision_payload"])

        decision_refs = [ref for result in results for ref in result.artifacts if ref.artifact_type == "decision.json"]
        if not decision_refs:
            raise TaskFailedError(f"Judge did not produce decision.json for {phase}")
        payload = parse_decision_file(decision_refs[-1].path)
        normalized = self.judge.normalize(phase, payload)
        normalized = self._apply_objective_gates_to_judge_decision(task_id, phase, round_id, normalized)
        self.repository.create_judge_decision(task_id, phase_id, phase, normalized)
        return normalized

    def _apply_objective_gates_to_judge_decision(
        self,
        task_id: str,
        phase: str,
        round_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if phase != TEST_JUDGEMENT:
            return payload
        objective_evidence = self._objective_gate_evidence(task_id, round_id)
        test_evidence = self._test_gate_evidence_for_round(task_id, round_id)
        objective_failure = self._objective_evidence_failure_reason(objective_evidence)
        if objective_failure:
            return {
                **payload,
                "decision": "fail",
                "tests_passed": False,
                "objective_gate_status": objective_evidence.get("status", "missing"),
                "evidence": {"objective_gate": objective_evidence, "test_gate": test_evidence},
                "reason": f"{objective_failure} LLM judge cannot override objective patch gate evidence.",
            }
        test_failure = self._test_evidence_failure_reason(test_evidence)
        if test_failure:
            return {
                **payload,
                "decision": "fail",
                "tests_passed": False,
                "test_gate_status": test_evidence.get("status", "missing"),
                "objective_gate_status": objective_evidence.get("status", "missing"),
                "evidence": {"objective_gate": objective_evidence, "test_gate": test_evidence},
                "reason": f"{test_failure} LLM judge cannot override Harness-run test evidence.",
            }
        return {
            **payload,
            "evidence": {
                **(payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}),
                "objective_gate": objective_evidence,
                "test_gate": test_evidence,
            },
        }

    def _objective_evidence_failure_reason(self, evidence: dict[str, Any]) -> str | None:
        if evidence.get("status") != "pass":
            return f"Objective gate status is {evidence.get('status', 'missing')}."
        checks = (
            ("legal_unified_diff", "Patch is not a legal unified diff."),
            ("patch_apply_check", "Patch apply check did not pass."),
            ("diff_check", "Patch diff check did not pass."),
            ("scope_ok", "Patch scope check did not pass."),
            ("size_ok", "Patch size/delete check did not pass."),
        )
        for key, reason in checks:
            if evidence.get(key) is not True:
                return reason
        if evidence.get("materialize_status") != "success":
            return f"Patch materialization status is {evidence.get('materialize_status', 'missing')}."
        return None

    def _test_evidence_failure_reason(self, evidence: dict[str, Any]) -> str | None:
        status = evidence.get("status")
        for field_name in ("build_exit_code", "test_exit_code"):
            exit_code = evidence.get(field_name)
            if exit_code is not None and exit_code != 0:
                return f"Harness {field_name} is {exit_code}."
        if status == "pass":
            return None
        if status == "skipped" and not self._require_harness_test_commands():
            return None
        if status == "missing":
            return "Harness test gate is missing."
        if status == "skipped":
            return "Harness test gate was skipped while test commands are required."
        return f"Harness test gate status is {status}."

    def _objective_gate_evidence(self, task_id: str, round_id: int) -> dict[str, Any]:
        content = self._latest_round_artifact_content(task_id, "objective_gate.md", round_id)
        if content is None:
            return {"status": "missing"}
        evidence = self._extract_evidence_json(content)
        status = self._markdown_field(content, "status") or "missing"
        if evidence:
            return {"status": status.lower(), **evidence}
        return {
            "status": status.lower(),
            "patch_apply_check": self._markdown_field(content, "patch_apply_status") == "pass",
            "materialize_status": self._markdown_field(content, "materialize_status") or "missing",
            "diff_check": self._markdown_field(content, "diff_check_status") == "pass",
            "legal_unified_diff": self._markdown_field(content, "legal_unified_diff") != "false",
            "scope_ok": self._markdown_field(content, "scope_status") == "pass",
            "size_ok": self._markdown_field(content, "size_status") in {None, "pass"},
        }

    def _test_gate_evidence_for_round(self, task_id: str, round_id: int) -> dict[str, Any]:
        content = self._latest_round_artifact_content(task_id, "test_gate.md", round_id)
        if content is None:
            return {"status": "missing"}
        evidence = self._extract_evidence_json(content)
        status = self._markdown_field(content, "status") or "missing"
        if evidence:
            return {"status": status.lower(), **evidence}
        return {"status": status.lower()}

    def _latest_round_artifact_content(self, task_id: str, artifact_type: str, round_id: int) -> str | None:
        for artifact in reversed(self.repository.list_artifacts(task_id, artifact_type)):
            path = Path(artifact["path"])
            if not path.exists() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if self._markdown_field(content, "round_id") != str(round_id):
                continue
            return content
        return None

    def _extract_evidence_json(self, content: str) -> dict[str, Any]:
        marker = "## Evidence JSON"
        marker_index = content.find(marker)
        if marker_index < 0:
            return {}
        block = content[marker_index + len(marker) :]
        match = re.search(r"```json\s*(.*?)```", block, re.DOTALL)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _objective_gate_status(self, task_id: str, round_id: int) -> str | None:
        for artifact in reversed(self.repository.list_artifacts(task_id, "objective_gate.md")):
            path = Path(artifact["path"])
            if not path.exists() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if self._markdown_field(content, "round_id") != str(round_id):
                continue
            status = self._markdown_field(content, "status")
            return status.lower() if status else None
        return None

    def _markdown_field(self, content: str, field_name: str) -> str | None:
        prefix = f"{field_name}:"
        for line in content.splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
        return None

    def _run_harness_test_gate(self, task_id: str, round_id: int) -> bool:
        repo_dir = self._latest_materialized_repo(task_id)
        commands = self._harness_test_commands(repo_dir)
        log_dir = self.artifact_manager.artifact_root / task_id / "context" / "test_gate_logs" / f"round_{round_id}"
        log_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any]] = []
        status = "skipped"
        if repo_dir is None:
            status = "fail"
            results.append({"command": "n/a", "exit_code": None, "stdout": "", "stderr": "No materialized repo exists."})
        elif commands:
            status = "pass"
            for index, command in enumerate(commands, start=1):
                stdout_path = log_dir / f"command_{index}.stdout.log"
                stderr_path = log_dir / f"command_{index}.stderr.log"
                try:
                    argv = self._harness_test_command_argv(command)
                    completed = subprocess.run(
                        argv,
                        cwd=repo_dir,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=int(self.config.get("testing", {}).get("timeout_seconds", 120)),
                    )
                    exit_code: int | str = completed.returncode
                    stdout = completed.stdout
                    stderr = completed.stderr
                except subprocess.TimeoutExpired as exc:
                    exit_code = "timeout"
                    stdout = self._timeout_output_to_text(exc.stdout)
                    stderr = self._timeout_output_to_text(exc.stderr)
                    stderr = (stderr + "\n" if stderr else "") + f"Command timed out after {exc.timeout}s."
                stdout_path.write_text(stdout, encoding="utf-8")
                stderr_path.write_text(stderr, encoding="utf-8")
                results.append(
                    {
                        "command": command,
                        "exit_code": exit_code,
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                    }
                )
                if exit_code != 0:
                    status = "fail"
        elif self._require_harness_test_commands():
            status = "fail"
            results.append({"command": "n/a", "exit_code": None, "stdout": "", "stderr": "No Harness test command configured or detected."})
        report = self._test_gate_report(task_id, round_id, repo_dir, status, results)
        self.artifact_manager.create_text_artifact(
            task_id,
            "test_gate.md",
            report,
            role="orchestrator",
            agent_id="test-gate",
        )
        return status == "pass"

    def _harness_test_command_argv(self, command: str) -> list[str]:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise TaskFailedError(f"Invalid Harness test command: {command!r}: {exc}") from exc
        if not argv:
            raise TaskFailedError("Invalid Harness test command: command is empty")
        return argv

    def _timeout_output_to_text(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def _require_harness_test_commands(self) -> bool:
        testing = self.config.get("testing", {})
        return bool(testing.get("require_commands", False)) if isinstance(testing, dict) else False

    def _harness_test_commands(self, repo_dir: Path | None) -> list[str]:
        testing = self.config.get("testing", {})
        configured = testing.get("commands") if isinstance(testing, dict) else None
        if isinstance(configured, list) and configured:
            return [str(command) for command in configured if str(command).strip()]
        if repo_dir is None:
            return []
        if (repo_dir / "tests").exists():
            return [f"{sys.executable} -m pytest -q"]
        if self._repo_has_python_files(repo_dir):
            return [f"{sys.executable} -m compileall -q ."]
        package_json = repo_dir / "package.json"
        if package_json.exists() and shutil.which("npm"):
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
            scripts = payload.get("scripts") if isinstance(payload, dict) else None
            if isinstance(scripts, dict) and scripts.get("test"):
                return ["npm test"]
            if isinstance(scripts, dict) and scripts.get("build"):
                return ["npm run build"]
        return []

    def _repo_has_python_files(self, repo_dir: Path) -> bool:
        for path in repo_dir.rglob("*.py"):
            if any(part in {".venv", "venv", "__pycache__"} for part in path.parts):
                continue
            return True
        return False

    def _test_gate_report(
        self,
        task_id: str,
        round_id: int,
        repo_dir: Path | None,
        status: str,
        results: list[dict[str, Any]],
    ) -> str:
        lines = [
            "# Harness Test Gate",
            "",
            f"status: {status}",
            f"task_id: {task_id}",
            f"round_id: {round_id}",
            f"repo_path: {repo_dir or 'none'}",
            "",
            "## Evidence JSON",
            "",
            "```json",
            json.dumps(self._test_gate_evidence(status, results), ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## Commands",
            "",
        ]
        if not results:
            lines.append("- none")
        for result in results:
            lines.extend(
                [
                    f"- command: {result['command']}",
                    f"  exit_code: {result['exit_code'] if result['exit_code'] is not None else 'n/a'}",
                    f"  stdout: {result['stdout'] or '-'}",
                    f"  stderr: {result['stderr'] or '-'}",
                ]
            )
        lines.append("")
        return "\n".join(lines)

    def _test_gate_evidence(self, status: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        exit_codes = [result.get("exit_code") for result in results if result.get("exit_code") is not None]
        numeric_exit_codes = [code for code in exit_codes if isinstance(code, int)]
        first_exit_code = exit_codes[0] if exit_codes else None
        return {
            "status": status,
            "build_exit_code": first_exit_code,
            "test_exit_code": 0 if numeric_exit_codes and all(code == 0 for code in numeric_exit_codes) else first_exit_code,
            "commands": [
                {
                    "command": result.get("command"),
                    "exit_code": result.get("exit_code"),
                    "stdout": result.get("stdout"),
                    "stderr": result.get("stderr"),
                }
                for result in results
            ],
        }

    def _test_gate_status(self, task_id: str, round_id: int) -> str | None:
        for artifact in reversed(self.repository.list_artifacts(task_id, "test_gate.md")):
            path = Path(artifact["path"])
            if not path.exists() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if self._markdown_field(content, "round_id") != str(round_id):
                continue
            status = self._markdown_field(content, "status")
            return status.lower() if status else None
        return None

    def _run_patch_merge(self, task_id: str, round_id: int, user_prompt: str) -> bool:
        self.run_role_phase(
            "executor",
            PATCH_MERGE,
            round_id,
            required_outputs_for("executor", PATCH_MERGE),
            user_prompt,
            agent_count_override=1,
        )
        return self._run_patch_validation(task_id, round_id)

    def _run_patch_validation(self, task_id: str, round_id: int) -> bool:
        latest = self._latest_merged_patch_for_round(task_id, round_id)
        if not latest:
            return False
        patch_path = Path(latest["path"])
        if not patch_path.exists():
            return False
        source_repo = self._source_repo_for_existing_project_task(task_id)
        gate_result = run_patch_gate(
            patch_path=patch_path,
            source_repo=source_repo,
            materialized_repo_dir=self._materialized_repo_dir(task_id, round_id),
            policy=self._patch_gate_policy(),
            copy_source=self._copy_source_for_patch_validation,
        )
        if gate_result.materialized_repo:
            self._write_materialized_success_marker(gate_result.materialized_repo, task_id, round_id, patch_path)
        report = patch_validation_markdown(gate_result)
        materialize_report = materialized_repo_markdown(gate_result, task_id, round_id)
        objective_report = objective_gate_markdown(gate_result, task_id, round_id)
        ref = self.artifact_manager.create_text_artifact(
            task_id,
            "patch_validation.md",
            report,
            phase_id=latest.get("phase_id"),
            role="orchestrator",
            agent_id="patch-validator",
        )
        materialized_ref = self.artifact_manager.create_text_artifact(
            task_id,
            "materialized_repo.md",
            materialize_report,
            phase_id=latest.get("phase_id"),
            role="orchestrator",
            agent_id="patch-materializer",
        )
        objective_ref = self.artifact_manager.create_text_artifact(
            task_id,
            "objective_gate.md",
            objective_report,
            phase_id=latest.get("phase_id"),
            role="orchestrator",
            agent_id="objective-gate",
        )
        self._emit(
            ProgressEvent(
                "patch_validated",
                task_id=task_id,
                phase=PATCH_MERGE,
                role="orchestrator",
                agent_id="patch-validator",
                round_id=round_id,
                status=gate_result.status.upper(),
                message=f"Objective patch gate {gate_result.status}",
                data={
                    "artifacts": 3,
                    "patch_validation": str(ref.path),
                    "materialized_repo_report": str(materialized_ref.path),
                    "objective_gate": str(objective_ref.path),
                    "materialized_repo": str(gate_result.materialized_repo) if gate_result.materialized_repo else "-",
                },
            )
        )
        return gate_result.status == "pass"

    def _patch_gate_policy(self) -> PatchGatePolicy:
        configured = self.config.get("patch_gate", {})
        if not isinstance(configured, dict):
            return PatchGatePolicy()
        return PatchGatePolicy(
            max_changed_lines=self._positive_int(configured.get("max_changed_lines"), 20_000, "patch_gate.max_changed_lines"),
            max_deleted_files=self._positive_int(configured.get("max_deleted_files"), 50, "patch_gate.max_deleted_files"),
        )

    def _latest_merged_patch_for_round(self, task_id: str, round_id: int) -> dict[str, Any] | None:
        patch_merge_phase_ids = {
            phase["phase_id"]
            for phase in self.repository.list_phases(task_id)
            if phase["phase_type"] == PATCH_MERGE and phase["round_id"] == round_id
        }
        if not patch_merge_phase_ids:
            return None
        candidates = [
            artifact
            for artifact in self.repository.list_artifacts(task_id, "merged_patch.diff")
            if artifact.get("phase_id") in patch_merge_phase_ids
        ]
        return candidates[-1] if candidates else None

    def _copy_source_for_patch_validation(self, source_repo: Path, destination: Path) -> None:
        shutil.copytree(
            source_repo,
            destination,
            ignore=lambda directory, names: {
                name
                for name in names
                if name in WorkspaceManager.DEFAULT_COPY_IGNORE_NAMES
                or self._is_relative_to((Path(directory) / name).resolve(), self.workspace_manager.workspace_root)
            },
        )

    def _materialized_repo_status(self, report: str) -> str:
        return self._materialized_repo_field(report, "status") or "unknown"

    def _materialized_repo_field(self, report: str, field_name: str) -> str | None:
        prefix = f"{field_name}: "
        for line in report.splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip().lower()
        return None

    def _write_materialized_success_marker(self, repo_dir: Path, task_id: str, round_id: int, patch_path: Path) -> None:
        marker = {
            "status": "success",
            "task_id": task_id,
            "round_id": round_id,
            "patch_path": str(patch_path),
            "patch_hash": sha256_file(patch_path) if patch_path.exists() else None,
        }
        (repo_dir / MATERIALIZED_SUCCESS_MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _backend_for(self, task_id: str | None, role: str) -> str:
        return self.config_service.backend_for(task_id, role)

    def _adapter_for_backend(self, backend: str) -> AgentAdapter:
        if backend == "mock":
            return MockAgentAdapter()
        if backend == "codex":
            return CodexCLIAdapter()
        if backend == "claude":
            return ClaudeCodeAdapter()
        if backend in {"gemini", "qwen"}:
            return HeadlessCLIAdapter(backend)
        raise ValueError(f"Unsupported agent backend: {backend}")

    def _source_repo_for_workspace(self) -> Path | None:
        if self._active_task_id:
            return self._source_repo_for_existing_project_task(self._active_task_id)
        if self._active_workflow_type in {BUGFIX, FEATURE_CHANGE}:
            return self._configured_source_repo()
        return None

    def _source_repo_for_existing_project_task(self, task_id: str) -> Path | None:
        if not self._task_uses_existing_project_source(task_id):
            return None
        return self._project_context_source_repo(task_id) or self._configured_source_repo()

    def _task_uses_existing_project_source(self, task_id: str) -> bool:
        if self._active_task_id == task_id and self._active_workflow_type:
            workflow_type = self._active_workflow_type
        else:
            task = self.repository.get_task(task_id)
            workflow_type = str(task.get("workflow_type") or NEW_PROJECT) if task else NEW_PROJECT
        return normalize_workflow_type(workflow_type) in {BUGFIX, FEATURE_CHANGE}

    def _project_context_source_repo(self, task_id: str) -> Path | None:
        for artifact in reversed(self.repository.list_artifacts(task_id, "project_context.md")):
            path = Path(artifact["path"])
            if not path.exists() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            for candidate in self._project_context_source_candidates(content):
                resolved = candidate.expanduser().resolve()
                if resolved.exists() and resolved.is_dir():
                    return resolved
        return None

    def _project_context_source_candidates(self, content: str) -> list[Path]:
        explicit_source_paths: list[Path] = []
        success_source_paths: list[Path] = []
        fallback_repo_paths: list[Path] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            for prefix in ("Historical source_repo:", "- source_repo:"):
                value = self._context_line_value(line, prefix)
                if value:
                    explicit_source_paths.append(Path(value))
            for prefix in (
                "Historical materialized_source:",
                "Historical materialized_source_candidate:",
                "Historical partial_materialized_source:",
                "- materialized_source:",
                "- materialized_source_candidate:",
                "- partial_materialized_source:",
            ):
                value = self._context_line_value(line, prefix)
                if value:
                    explicit_source_paths.append(Path(value))
            for prefix in ("Historical success_path:", "- success_path:"):
                value = self._context_line_value(line, prefix)
                if value:
                    success_source_paths.append(Path(value) / "source")
            for prefix in ("Historical latest_agent_repo_workspace:", "- latest_agent_repo_workspace:"):
                value = self._context_line_value(line, prefix)
                if value:
                    fallback_repo_paths.append(Path(value))
        return explicit_source_paths + success_source_paths + fallback_repo_paths

    def _context_line_value(self, line: str, prefix: str) -> str | None:
        if not line.startswith(prefix):
            return None
        value = line[len(prefix) :].strip()
        return value or None

    def _effective_agent_count(self, task_id: str, role: str, phase: str, agent_count_override: int | None = None) -> int:
        if role == "executor" and phase in SINGLE_EXECUTOR_FIX_PHASES:
            # Intentional hard rule for future AI maintainers: fix phases use one executor.
            # Multiple fix executors produce competing patches for the same defect, which
            # increases merge conflicts, context size, and risk of broad accidental changes.
            return 1
        if agent_count_override is not None:
            return agent_count_override
        return self.config_service.role_count(task_id, role)

    def _should_use_materialized_repo(self, role: str, phase: str) -> bool:
        if role == "executor":
            return phase in {FIXING, REVIEW_FIXING}
        if role == "tester":
            return phase in {TESTING, REGRESSION_TESTING}
        if role == "reviewer":
            return phase != PLAN_REVIEW
        if role == "judge":
            return phase != PLAN_JUDGEMENT
        if role == "communicator":
            return True
        return False

    def _prepare_materialized_workspace_repo(self, task_id: str, role: str, phase: str, repo_dir: Path) -> None:
        if not self._should_use_materialized_repo(role, phase):
            return
        materialized_repo = self._latest_materialized_repo(task_id)
        if not materialized_repo:
            return
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        shutil.copytree(materialized_repo, repo_dir, ignore=self._copy_ignore_for_materialized_workspace)

    def _copy_ignore_for_materialized_workspace(self, directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", MATERIALIZED_SUCCESS_MARKER}
        }

    def _repo_context_metadata(self, task_id: str, role: str, phase: str) -> dict[str, Any]:
        if self._should_use_materialized_repo(role, phase):
            materialized_repo = self._latest_materialized_repo(task_id)
            if materialized_repo:
                return {
                    "repository_source_type": "materialized_merged_patch",
                    "repository_source_path": str(materialized_repo),
                    "repository_source_note": "This role's repository directory was copied from the latest Harness materialized merged patch.",
                }
        project_context_source_repo = (
            self._project_context_source_repo(task_id)
            if self._task_uses_existing_project_source(task_id)
            else None
        )
        if project_context_source_repo:
            return {
                "repository_source_type": "project_context_source_repo",
                "repository_source_path": str(project_context_source_repo),
                "repository_source_note": "This role's repository directory was copied from the source repo selected from project_context.md.",
            }
        source_repo = self._source_repo_for_workspace()
        if source_repo:
            return {
                "repository_source_type": "configured_source_repo",
                "repository_source_path": str(source_repo),
            }
        return {"repository_source_type": "empty_workspace_repo"}

    def _materialized_root(self, task_id: str) -> Path:
        return self.workspace_manager.workspace_root / task_id / "_materialized"

    def _materialized_repo_dir(self, task_id: str, round_id: int) -> Path:
        return self._materialized_root(task_id) / f"round_{round_id}" / "repo"

    def _latest_materialized_repo(self, task_id: str) -> Path | None:
        latest_success_round = self._latest_successful_materialized_round_from_artifacts(task_id)
        if latest_success_round is None:
            return None
        root = self._materialized_root(task_id)
        if not root.exists():
            return None
        candidate = self._materialized_repo_dir(task_id, latest_success_round)
        if not self._materialized_success_marker_ok(candidate, task_id, latest_success_round):
            return None
        return candidate

    def _materialized_round_number(self, path: Path) -> int:
        try:
            return int(path.name.removeprefix("round_"))
        except ValueError:
            return -1

    def _latest_successful_materialized_round_from_artifacts(self, task_id: str) -> int | None:
        artifacts = self.repository.list_artifacts(task_id, "materialized_repo.md")
        for artifact in reversed(artifacts):
            path = Path(artifact["path"])
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if self._materialized_repo_status(text) != "success":
                return None
            round_id = self._extract_materialized_report_round(text)
            return round_id
        return None

    def _extract_materialized_report_round(self, report: str) -> int | None:
        for line in report.splitlines():
            if line.startswith("round_id: "):
                try:
                    return int(line.split(":", 1)[1].strip())
                except ValueError:
                    return None
        return None

    def _materialized_success_marker_ok(self, repo_dir: Path, task_id: str, round_id: int) -> bool:
        marker_path = repo_dir / MATERIALIZED_SUCCESS_MARKER
        if not repo_dir.is_dir() or not marker_path.is_file():
            return False
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        return (
            payload.get("status") == "success"
            and payload.get("task_id") == task_id
            and int(payload.get("round_id", -1)) == round_id
        )

    def _single_active_task_id(self, user_prompt: str | None) -> str:
        if self._active_task_id:
            return self._active_task_id
        if user_prompt is not None:
            task_id = self.repository.latest_task_id(user_prompt)
            if task_id:
                return task_id
        task_id = self.repository.latest_task_id()
        if not task_id:
            raise TaskFailedError("No task exists")
        return task_id

    def _emit(self, event: ProgressEvent) -> None:
        if self.progress_callback:
            self.progress_callback(event)

    def _context_metadata(self, task: dict[str, Any], role: str, phase: str) -> dict[str, Any]:
        if role != "communicator" or phase != DELIVERY:
            return {}
        task_id = str(task["task_id"])
        expected_success_path = self._delivery_project_dir(task_id, str(task["user_prompt"]))
        return {
            "expected_success_path": str(expected_success_path),
            "expected_final_delivery": str(expected_success_path / "final_delivery.md"),
            "expected_usage_guide": str(expected_success_path / "usage_guide.md"),
            "expected_artifacts_manifest": str(expected_success_path / "artifacts_manifest.md"),
            "publish_timing": "Harness will publish these files after the communicator role succeeds.",
        }

    def _workflow_prompt(self, user_prompt: str, workflow_type: str) -> str:
        if workflow_type == BUGFIX:
            instruction = (
                "Workflow classification: bugfix.\n"
                "Use the repair workflow. Preserve existing behavior except where needed to fix the reported issue. "
                "Keep the change scope minimal, produce fix artifacts, and rely on tester and judge artifacts to "
                "decide whether the fix is complete. Do not perform full new-project planning."
            )
        elif workflow_type == FEATURE_CHANGE:
            instruction = (
                "Workflow classification: feature_change.\n"
                "Use the feature-change workflow. The planner must evaluate compatibility with existing behavior, "
                "identify the blast radius, define acceptance criteria, and produce a local task breakdown before "
                "execution. Executors must implement only the approved scope."
            )
        elif workflow_type == MISC:
            instruction = (
                "Workflow classification: misc.\n"
                "Use the miscellaneous response workflow. Answer the user's question or provide requested analysis "
                "without creating or modifying project files. Use any provided historical context as reference only."
            )
        else:
            instruction = (
                "Workflow classification: new_project.\n"
                "Use the full new-project workflow from planning through final delivery. Establish project structure, "
                "implementation approach, validation strategy, and final handoff artifacts."
            )
        return f"{instruction}\n\nOriginal user prompt:\n{user_prompt}"

    def _stage_input_artifacts(
        self,
        task_id: str,
        input_dir: Path,
        role: str,
        phase: str,
        exclude_phase_id: str | None = None,
        round_id: int | None = None,
        current_agent_id: str | None = None,
    ) -> list[Path]:
        artifacts = self.repository.list_artifacts(task_id)
        phases_by_id = {phase_row["phase_id"]: phase_row for phase_row in self.repository.list_phases(task_id)}
        staged_dir = input_dir / "artifacts"
        staged_dir.mkdir(parents=True, exist_ok=True)
        staged_paths: list[Path] = []
        manifest_lines = ["# Input Artifact Manifest", ""]
        target_role = role
        limits = self._artifact_input_limits()
        staged_file_count = 0
        staged_total_bytes = 0
        visible_artifacts = [
            artifact
            for artifact in artifacts
            if not (exclude_phase_id and artifact["phase_id"] == exclude_phase_id)
        ]
        visible_artifacts = self.artifact_visibility.filter_visible_artifacts(
            visible_artifacts,
            phases_by_id,
            role,
            phase,
            round_id,
            current_agent_id=current_agent_id,
        )
        manifest_lines.extend(
            self._testing_failure_context_manifest_lines(
                task_id,
                artifacts,
                phases_by_id,
                role,
                phase,
                round_id,
            )
        )
        for index, artifact in enumerate(reversed(visible_artifacts), start=1):
            source = Path(artifact["path"])
            if not source.exists():
                continue
            source_size = source.stat().st_size
            staging_mode = self._artifact_staging_mode(role, phase, artifact, source)
            if staging_mode == "path_only":
                self._append_path_only_artifact_manifest(
                    manifest_lines,
                    index,
                    artifact,
                    source,
                    "large artifact indexed by path only to avoid repeating full content in model context",
                )
                continue
            if staged_file_count >= limits["max_files"]:
                self._append_skipped_artifact_manifest(manifest_lines, index, artifact, source, "max_files exceeded")
                continue
            remaining_total_bytes = limits["max_total_bytes"] - staged_total_bytes
            if remaining_total_bytes <= 0:
                self._append_skipped_artifact_manifest(manifest_lines, index, artifact, source, "max_total_bytes exceeded")
                continue
            safe_type = artifact["artifact_type"].replace("/", "__").replace(" ", "_")
            artifact_role = artifact["role"] or "unknown"
            agent_id = artifact["agent_id"] or "unknown"
            version = artifact["version"]
            destination = staged_dir / f"{index:03d}_{artifact_role}_{agent_id}_{safe_type}_v{version}_{source.name}"
            copied_bytes, truncated = self._copy_artifact_with_budget(
                source,
                destination,
                max_file_bytes=self._artifact_max_file_bytes(limits["max_file_bytes"], staging_mode),
                remaining_total_bytes=remaining_total_bytes,
            )
            staged_total_bytes += copied_bytes
            staged_file_count += 1
            staged_paths.append(destination)
            manifest_lines.extend(
                [
                    f"## {index}. {artifact['artifact_type']} v{version}",
                    f"- local_path: {destination}",
                    f"- source_path: {source}",
                    f"- role: {artifact_role}",
                    f"- agent_id: {agent_id}",
                    f"- phase_id: {artifact['phase_id']}",
                    f"- source_bytes: {source_size}",
                    f"- staged_bytes: {copied_bytes}",
                    f"- truncated: {str(truncated).lower()}",
                    "",
                ]
            )
        manifest_path = input_dir / "manifest.md"
        manifest_path.write_text("\n".join(manifest_lines), encoding="utf-8")
        return [manifest_path, *staged_paths]

    def _testing_failure_context_manifest_lines(
        self,
        task_id: str,
        artifacts: list[dict[str, Any]],
        phases_by_id: dict[str, dict[str, Any]],
        role: str,
        phase: str,
        round_id: int | None,
    ) -> list[str]:
        if role != "executor" or phase not in {FIXING, REVIEW_FIXING} or round_id is None:
            return []
        failed_rounds = self._failed_test_rounds_before(task_id, phases_by_id, round_id)
        if not failed_rounds:
            return []
        tester_artifacts_by_round: dict[int, set[str]] = {}
        for artifact in artifacts:
            if (artifact.get("role") or "") != "tester" or artifact.get("artifact_type") not in TEST_REPORT_ARTIFACTS:
                continue
            phase_row = phases_by_id.get(artifact.get("phase_id") or "")
            if not phase_row or phase_row.get("round_id") is None:
                continue
            artifact_round = int(phase_row["round_id"])
            if artifact_round >= round_id:
                continue
            tester_artifacts_by_round.setdefault(artifact_round, set()).add(str(artifact["artifact_type"]))
        complete_visible_rounds = [
            test_round
            for test_round, artifact_types in tester_artifacts_by_round.items()
            if TEST_REPORT_ARTIFACTS <= artifact_types
        ]
        latest_visible_round = max(complete_visible_rounds, default=None)
        unavailable_failed_rounds = [
            test_round
            for test_round in failed_rounds
            if not TEST_REPORT_ARTIFACTS <= tester_artifacts_by_round.get(test_round, set())
        ]
        lines = [
            "## Harness Test Failure Context",
            f"- failed_test_round_count_before_current: {len(failed_rounds)}",
            f"- failed_test_round_ids_before_current: {', '.join(str(value) for value in failed_rounds)}",
        ]
        if latest_visible_round is not None:
            lines.append(f"- latest_visible_complete_test_evidence_round: {latest_visible_round}")
        else:
            lines.append("- latest_visible_complete_test_evidence_round: none")
        if unavailable_failed_rounds:
            lines.append(
                "- failed_test_rounds_without_complete_visible_reports: "
                + ", ".join(str(value) for value in unavailable_failed_rounds)
            )
            lines.append(
                "- evidence_note: Some failed test rounds did not publish complete tester report artifacts; "
                "use the latest visible test reports together with the current repo state."
            )
        lines.append("")
        return lines

    def _failed_test_rounds_before(
        self,
        task_id: str,
        phases_by_id: dict[str, dict[str, Any]],
        round_id: int,
    ) -> list[int]:
        failed_rounds: set[int] = set()
        for phase_row in phases_by_id.values():
            if phase_row.get("task_id") != task_id:
                continue
            if phase_row.get("phase_type") not in {TESTING, REGRESSION_TESTING}:
                continue
            if phase_row.get("round_id") is None or int(phase_row["round_id"]) >= round_id:
                continue
            if phase_row.get("status") == FAILED:
                failed_rounds.add(int(phase_row["round_id"]))
        for decision in self.repository.list_judge_decisions(task_id):
            if decision.get("decision_type") != TEST_JUDGEMENT:
                continue
            phase_row = phases_by_id.get(decision.get("phase_id") or "")
            if not phase_row or phase_row.get("round_id") is None:
                continue
            decision_round = int(phase_row["round_id"])
            if decision_round >= round_id:
                continue
            try:
                payload = json.loads(decision["decision_payload"])
            except Exception:
                failed_rounds.add(decision_round)
                continue
            if not self.judge.is_test_pass(payload):
                failed_rounds.add(decision_round)
        return sorted(failed_rounds)

    def _artifact_input_limits(self) -> dict[str, int]:
        configured = self.config.get("artifact_input", {})
        return {
            "max_files": self._positive_int(configured.get("max_files"), 50, "artifact_input.max_files"),
            "max_file_bytes": self._positive_int(
                configured.get("max_file_bytes"), 262_144, "artifact_input.max_file_bytes"
            ),
            "max_total_bytes": self._positive_int(
                configured.get("max_total_bytes"), 1_048_576, "artifact_input.max_total_bytes"
            ),
        }

    def _positive_int(self, value: Any, default: int, field_name: str) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer > 0, got {value!r}") from exc
        if parsed <= 0:
            raise ValueError(f"{field_name} must be an integer > 0, got {parsed}")
        return parsed

    def _copy_artifact_with_budget(
        self,
        source: Path,
        destination: Path,
        *,
        max_file_bytes: int,
        remaining_total_bytes: int,
    ) -> tuple[int, bool]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_size = source.stat().st_size
        allowed_bytes = min(max_file_bytes, remaining_total_bytes)
        if source_size <= allowed_bytes:
            shutil.copy2(source, destination)
            return source_size, False
        raw = source.read_bytes()
        if allowed_bytes <= 128:
            selected = raw[:allowed_bytes]
        else:
            marker = b"\n\n...[artifact truncated by Harness input budget]...\n\n"
            head_size = max(1, (allowed_bytes - len(marker)) // 2)
            tail_size = max(1, allowed_bytes - len(marker) - head_size)
            selected = raw[:head_size] + marker + raw[-tail_size:]
        destination.write_text(selected.decode("utf-8", errors="replace"), encoding="utf-8")
        return destination.stat().st_size, True

    def _append_skipped_artifact_manifest(
        self,
        manifest_lines: list[str],
        index: int,
        artifact: dict[str, Any],
        source: Path,
        reason: str,
    ) -> None:
        manifest_lines.extend(
            [
                f"## {index}. {artifact['artifact_type']} v{artifact['version']}",
                f"- skipped: true",
                f"- reason: {reason}",
                f"- source_path: {source}",
                f"- role: {artifact['role'] or 'unknown'}",
                f"- agent_id: {artifact['agent_id'] or 'unknown'}",
                f"- phase_id: {artifact['phase_id']}",
                f"- source_bytes: {source.stat().st_size}",
                "",
            ]
        )

    def _append_path_only_artifact_manifest(
        self,
        manifest_lines: list[str],
        index: int,
        artifact: dict[str, Any],
        source: Path,
        reason: str,
    ) -> None:
        manifest_lines.extend(
            [
                f"## {index}. {artifact['artifact_type']} v{artifact['version']}",
                f"- local_path: path_only",
                f"- full_content_staged: false",
                f"- reason: {reason}",
                f"- source_path: {source}",
                f"- role: {artifact['role'] or 'unknown'}",
                f"- agent_id: {artifact['agent_id'] or 'unknown'}",
                f"- phase_id: {artifact['phase_id']}",
                f"- source_bytes: {source.stat().st_size}",
                "",
            ]
        )

    def _artifact_staging_mode(self, role: str, phase: str, artifact: dict[str, Any], source: Path) -> str:
        if artifact["artifact_type"] != "merged_patch.diff":
            return "copy"
        if source.stat().st_size < 64_000:
            return "copy"
        if role in {"tester", "judge", "communicator"}:
            return "path_only"
        if role == "reviewer":
            return "truncated"
        if role == "executor" and phase in {FIXING, REVIEW_FIXING}:
            return "truncated"
        return "copy"

    def _artifact_max_file_bytes(self, configured_max_file_bytes: int, staging_mode: str) -> int:
        if staging_mode == "truncated":
            return min(configured_max_file_bytes, 16_384)
        return configured_max_file_bytes

    def _publish_delivery(self, task_id: str, final_path: Path) -> Path:
        task = self.repository.get_task(task_id)
        prompt = task["user_prompt"] if task else task_id
        deliver_root = Path(self.config["system"].get("deliver_root", "./deliver")).expanduser().resolve()
        project_dir = self._delivery_project_dir(task_id, prompt, deliver_root)
        project_dir.mkdir(parents=True, exist_ok=True)
        destination = project_dir / "final_delivery.md"
        shutil.copy2(final_path, destination)
        usage_guide = self.communicator.latest_usage_guide(task_id)
        if usage_guide and usage_guide.exists():
            shutil.copy2(usage_guide, project_dir / "usage_guide.md")
        copied_artifacts = self._publish_supporting_artifacts(task_id, project_dir)
        source_files = self._publish_materialized_source(task_id, project_dir)
        dependency_files = self._publish_dependency_installer(project_dir)
        success_path = self._write_success_path(task_id, project_dir, destination, usage_guide)
        manifest = project_dir / "artifacts_manifest.md"
        lines = [
            "# Delivery Artifact Manifest",
            "",
            f"task_id: {task_id}",
            f"success_path: {project_dir}",
            f"source_final_delivery: {final_path}",
            f"published_final_delivery: {destination}",
            "",
            "## Published Files",
            "",
            f"- final_delivery.md: {destination}",
            f"- success_path.md: {success_path}",
        ]
        if usage_guide and usage_guide.exists():
            lines.append(f"- usage_guide.md: {project_dir / 'usage_guide.md'}")
        if (project_dir / "patches" / "final.patch").exists():
            lines.append(f"- patches/final.patch: {project_dir / 'patches' / 'final.patch'}")
        if source_files:
            lines.append(f"- source/: {project_dir / 'source'}")
        for dependency_file in dependency_files:
            lines.append(f"- {dependency_file.relative_to(project_dir)}: {dependency_file}")
        if copied_artifacts:
            lines.extend(["", "## Supporting Artifacts", ""])
            for artifact_type, path in copied_artifacts:
                lines.append(f"- {artifact_type}: {path}")
        if source_files:
            lines.extend(["", "## Materialized Source Files", ""])
            for path in source_files:
                lines.append(f"- {path.relative_to(project_dir)}")
        else:
            lines.extend(
                [
                    "",
                    "## Materialized Source Files",
                    "",
                    "- none: no safely materializable new-file patch was found. Use `patches/final.patch` with the target repository.",
                ]
            )
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._record_published_artifact(task_id, "success_path.md", success_path)
        self._record_published_artifact(task_id, "artifacts_manifest.md", manifest)
        for dependency_file in dependency_files:
            self._record_published_artifact(task_id, dependency_file.name, dependency_file)
        return destination

    def delivery_success_path(self, task_id: str) -> Path | None:
        task = self.repository.get_task(task_id)
        if not task:
            return None
        deliver_root = Path(self.config["system"].get("deliver_root", "./deliver")).expanduser().resolve()
        project_dir = self._delivery_project_dir(task_id, task["user_prompt"], deliver_root)
        return project_dir if project_dir.exists() else None

    def _delivery_project_dir(self, task_id: str, prompt: str, deliver_root: Path | None = None) -> Path:
        root = deliver_root or Path(self.config["system"].get("deliver_root", "./deliver")).expanduser().resolve()
        return root / f"{self._slugify_project_name(prompt)}-{task_id[:8]}"

    def _write_success_path(self, task_id: str, project_dir: Path, final_delivery: Path, usage_guide: Path | None) -> Path:
        path = project_dir / "success_path.md"
        lines = [
            "# Success Path",
            "",
            f"task_id: {task_id}",
            f"success_path: {project_dir}",
            f"final_delivery: {final_delivery}",
        ]
        if usage_guide and usage_guide.exists():
            lines.append(f"usage_guide: {project_dir / 'usage_guide.md'}")
        lines.append(f"artifacts_manifest: {project_dir / 'artifacts_manifest.md'}")
        lines.extend(
            [
                "",
                "Open this directory to inspect the delivered result and supporting artifacts.",
                "If the Web viewer is running, select the same task_id to inspect role rounds and role artifacts.",
            ]
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _record_published_artifact(self, task_id: str, artifact_type: str, path: Path) -> None:
        if not path.exists() or not path.is_file():
            return
        version = self.repository.next_artifact_version(task_id, artifact_type)
        self.repository.create_artifact(
            ArtifactRef(
                artifact_id=f"published-{task_id}-{artifact_type}-{version}",
                task_id=task_id,
                phase_id=None,
                role="orchestrator",
                agent_id="harness",
                artifact_type=artifact_type,
                path=path,
                version=version,
                hash=sha256_file(path),
            )
        )

    def _publish_supporting_artifacts(self, task_id: str, project_dir: Path) -> list[tuple[str, Path]]:
        artifact_types = [
            "merged_patch.diff",
            "merged_patch_metadata.md",
            "patch_validation.md",
            "materialized_repo.md",
            "objective_gate.md",
            "test_gate.md",
            "merge_report.md",
            "patch.diff",
            "fix_patch.diff",
            "patch_metadata.md",
            "implementation_plan.md",
            "changed_files.md",
            "self_check.md",
            "fix_schedule.md",
            "fix_notes.md",
            "build_report.md",
            "test_report.md",
            "bug_report.md",
            "review_report.md",
            "decision_summary.md",
        ]
        copied: list[tuple[str, Path]] = []
        artifact_dir = project_dir / "artifacts"
        patch_dir = project_dir / "patches"
        for artifact_type in artifact_types:
            artifacts = self.repository.list_artifacts(task_id, artifact_type)
            if not artifacts:
                continue
            source = Path(artifacts[-1]["path"])
            if not source.exists():
                continue
            destination = artifact_dir / self._safe_deliver_filename(artifact_type)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append((artifact_type, destination))
        final_patch_ref = self._latest_patch_artifact(task_id)
        if final_patch_ref:
            source = Path(final_patch_ref["path"])
            if source.exists():
                patch_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, patch_dir / "final.patch")
        return copied

    def _publish_materialized_source(self, task_id: str, project_dir: Path) -> list[Path]:
        patch_path = project_dir / "patches" / "final.patch"
        source_dir = project_dir / "source"
        if source_dir.exists():
            shutil.rmtree(source_dir)
        materialized_repo = self._latest_materialized_repo(task_id)
        if materialized_repo:
            shutil.copytree(materialized_repo, source_dir, ignore=self._copy_ignore_for_publish)
            return sorted(path for path in source_dir.rglob("*") if path.is_file())
        if not patch_path.exists():
            return []
        files = self._materialized_files_from_unified_diff(
            patch_path.read_text(encoding="utf-8", errors="replace"),
            self._source_repo_for_existing_project_task(task_id),
            include_modified=True,
        )
        if not files:
            return []
        written: list[Path] = []
        for relative_name, lines in sorted(files.items()):
            if not self._is_safe_relative_path(relative_name):
                continue
            destination = source_dir / relative_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
            written.append(destination)
        return written

    def _publish_dependency_installer(self, project_dir: Path) -> list[Path]:
        source_dir = project_dir / "source" if (project_dir / "source").is_dir() else project_dir
        if not source_dir.exists():
            return []
        written: list[Path] = []
        dependency_file = next(
            (path for path in (source_dir / "requirements.txt", source_dir / "request.txt") if path.exists()),
            None,
        )
        install_command = ""
        if (source_dir / "pyproject.toml").exists():
            install_command = '.venv/bin/python -m pip install -e ".[dev]"'
        if dependency_file is None:
            inferred_dependencies = self._infer_delivery_python_dependencies(source_dir, project_dir)
            if inferred_dependencies:
                dependency_file = source_dir / "requirements.txt"
                dependency_file.write_text("\n".join(inferred_dependencies) + "\n", encoding="utf-8")
                written.append(dependency_file)
        if not install_command and dependency_file is not None:
            install_command = f".venv/bin/python -m pip install -r {dependency_file.name}"
        if not install_command:
            return []
        installer = source_dir / "install_dependencies.sh"
        installer.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'cd "$(dirname "$0")"',
                    'PYTHON_BIN="${PYTHON_BIN:-python3}"',
                    'if [ ! -d ".venv" ]; then',
                    '  "$PYTHON_BIN" -m venv .venv',
                    "fi",
                    ".venv/bin/python -m pip install --upgrade pip",
                    install_command,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        installer.chmod(0o755)
        written.append(installer)
        return written

    def _infer_delivery_python_dependencies(self, source_dir: Path, project_dir: Path) -> list[str]:
        if not any(source_dir.rglob("*.py")):
            return []
        text_parts: list[str] = []
        for path in (
            project_dir / "usage_guide.md",
            project_dir / "final_delivery.md",
            source_dir / "README.md",
            source_dir / "readme.md",
        ):
            if path.exists() and path.is_file():
                text_parts.append(path.read_text(encoding="utf-8", errors="replace").lower())
        text = "\n".join(text_parts)
        dependencies: list[str] = []
        has_pytest_signal = (
            (source_dir / "tests").exists()
            or "python -m pytest" in text
            or "python3 -m pytest" in text
            or re.search(r"(^|\s)pytest(\s|$)", text) is not None
        )
        if has_pytest_signal:
            dependencies.append("pytest")
        if "--cov" in text or "pytest-cov" in text:
            dependencies.append("pytest-cov")
        return dependencies

    def _copy_ignore_for_publish(self, directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", MATERIALIZED_SUCCESS_MARKER}
        }

    def _new_files_from_unified_diff(self, patch_text: str) -> dict[Path, list[str]]:
        return self._materialized_files_from_unified_diff(patch_text, source_repo=None, include_modified=False)

    def _materialized_files_from_unified_diff(
        self,
        patch_text: str,
        source_repo: Path | None,
        *,
        include_modified: bool,
    ) -> dict[Path, list[str]]:
        files: dict[Path, list[str]] = {}
        current_path: Path | None = None
        old_path: Path | None = None
        current_lines: list[str] = []
        current_is_new_file = False
        current_is_deleted_file = False
        base_lines: list[str] = []
        cursor = 0
        in_hunk = False

        def flush() -> None:
            nonlocal current_path, old_path, current_lines, current_is_new_file, current_is_deleted_file, base_lines, cursor, in_hunk
            if current_path and not current_is_deleted_file and (
                current_is_new_file or (include_modified and (base_lines or current_lines))
            ):
                if not current_is_new_file and base_lines:
                    current_lines.extend(base_lines[cursor:])
                files[current_path] = current_lines
            current_path = None
            old_path = None
            current_lines = []
            current_is_new_file = False
            current_is_deleted_file = False
            base_lines = []
            cursor = 0
            in_hunk = False

        for line in patch_text.splitlines():
            if line.startswith("diff --git "):
                flush()
                continue
            if line == "--- /dev/null":
                current_is_new_file = True
                continue
            if line.startswith("--- "):
                target = self._strip_diff_path(line[4:].strip())
                if target != Path("/dev/null"):
                    old_path = target
                continue
            if line.startswith("+++ "):
                target = self._strip_diff_path(line[4:].strip())
                if target == Path("/dev/null"):
                    current_is_deleted_file = True
                    continue
                current_path = target
                if not current_is_new_file:
                    base_path = old_path or current_path
                    if source_repo and self._is_safe_relative_path(base_path):
                        source_file = source_repo / base_path
                        if source_file.exists() and source_file.is_file():
                            base_lines = source_file.read_text(encoding="utf-8", errors="replace").splitlines()
                continue
            if line.startswith("@@"):
                hunk_start = self._parse_old_hunk_start(line)
                if hunk_start is not None and base_lines and not current_is_new_file:
                    target_index = max(0, hunk_start - 1)
                    current_lines.extend(base_lines[cursor:target_index])
                    cursor = target_index
                in_hunk = True
                continue
            if in_hunk and current_path:
                if line.startswith("\\ No newline at end of file"):
                    continue
                if current_is_new_file:
                    if line.startswith("+") and not line.startswith("+++"):
                        current_lines.append(line[1:])
                    continue
                if line.startswith(" ") and base_lines:
                    current_lines.append(line[1:])
                    cursor += 1
                elif line.startswith("-") and not line.startswith("---"):
                    cursor += 1
                elif line.startswith("+") and not line.startswith("+++"):
                    current_lines.append(line[1:])
        flush()
        return files

    def _strip_diff_path(self, raw_path: str) -> Path:
        if raw_path == "/dev/null":
            return Path("/dev/null")
        path = raw_path.split("\t", 1)[0].split(" ", 1)[0]
        if path.startswith(("a/", "b/")):
            path = path[2:]
        return Path(path)

    def _parse_old_hunk_start(self, header: str) -> int | None:
        match = re.search(r"@@ -(\d+)", header)
        return int(match.group(1)) if match else None

    def _configured_source_repo(self) -> Path | None:
        source_repo = self.config.get("system", {}).get("source_repo")
        if not source_repo:
            return None
        path = Path(str(source_repo)).expanduser().resolve()
        return path if path.exists() and path.is_dir() else None

    def _latest_patch_artifact(self, task_id: str) -> dict[str, Any] | None:
        for artifact_type in ("merged_patch.diff", "fix_patch.diff", "patch.diff"):
            artifacts = self.repository.list_artifacts(task_id, artifact_type)
            if artifacts:
                return artifacts[-1]
        return None

    def _safe_deliver_filename(self, artifact_type: str) -> str:
        return artifact_type.replace("/", "__").replace("\\", "__").replace(" ", "_")

    def _is_safe_relative_path(self, path: Path) -> bool:
        return not path.is_absolute() and ".." not in path.parts

    def _is_relative_to(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _slugify_project_name(self, prompt: str) -> str:
        ascii_prompt = prompt.encode("ascii", "ignore").decode("ascii").lower()
        compact = re.sub(r"[^a-z0-9]+", "-", ascii_prompt).strip("-")
        compact = re.sub(r"-+", "-", compact)[:32].strip("-")
        return compact or "project"
