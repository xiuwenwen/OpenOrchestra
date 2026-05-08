from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from harness.adapters.base import AgentAdapter
from harness.adapters.claude_code_adapter import ClaudeCodeAdapter
from harness.adapters.codex_cli_adapter import CodexCLIAdapter
from harness.adapters.headless_cli_adapter import HeadlessCLIAdapter
from harness.adapters.mock_adapter import MockAgentAdapter
from harness.agents.result import AgentRunResult
from harness.agents.runner import AgentPhaseRunner
from harness.artifacts.manager import ArtifactManager
from harness.artifacts.schemas import required_outputs_for
from harness.artifacts.validator import ArtifactValidator
from harness.artifacts.visibility import ARTIFACT_VISIBILITY_RULES, ArtifactVisibilityPolicy
from harness.communication.communicator import Communicator
from harness.config.loader import load_config
from harness.config.runtime import RuntimeConfigService
from harness.context.staging import InputStagingService
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
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REVIEW_FIXING,
    REVIEW_JUDGEMENT,
    REVIEWING,
    RUNNING,
    TEST_JUDGEMENT,
)
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, MISC, NEW_PROJECT, normalize_workflow_type
from harness.judge.decision_parser import parse_decision_file
from harness.judge.judge_runner import MockJudge
from harness.logs.logger import get_logger
from harness.gates.patch_gate import PatchGateService
from harness.gates.test_gate import TestGateService
from harness.materialization.service import MaterializedRepoService
from harness.prompts.builder import PromptBuilder
from harness.state.db import StateDB
from harness.state.repository import StateRepository
from harness.workflow.delivery import DeliveryPublisher
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
        self.materialized_repo_service = MaterializedRepoService(
            self.repository,
            self.workspace_manager,
            config=self.config,
            markdown_field=self._markdown_field,
            active_task_id=lambda: self._active_task_id,
            active_workflow_type=lambda: self._active_workflow_type,
        )
        self.test_gate_service = TestGateService(
            config=self.config,
            repository=self.repository,
            artifact_manager=self.artifact_manager,
            latest_materialized_repo=self.materialized_repo_service.latest_materialized_repo,
            markdown_field=self._markdown_field,
        )
        self.patch_gate_service = PatchGateService(
            config=self.config,
            repository=self.repository,
            artifact_manager=self.artifact_manager,
            source_repo_for_task=self.materialized_repo_service.source_repo_for_existing_project_task,
            materialized_repo_dir=self.materialized_repo_service.materialized_repo_dir,
            copy_source=self.materialized_repo_service.copy_source_for_patch_validation,
            write_success_marker=self.materialized_repo_service.write_materialized_success_marker,
            emit=self._emit,
            positive_int=self._positive_int,
        )
        self.input_staging_service = InputStagingService(
            config=self.config,
            repository=self.repository,
            visibility=self.artifact_visibility,
            judge=self.judge,
            repo_context_metadata=self.materialized_repo_service.repo_context_metadata,
            positive_int=self._positive_int,
        )
        self.agent_runner = AgentPhaseRunner(self)
        self.delivery_publisher = DeliveryPublisher(self)
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
        self.test_gate_service.latest_materialized_repo = self._latest_materialized_repo
        return self.test_gate_service.run(task_id, round_id)

    def _harness_test_command_argv(self, command: str) -> list[str]:
        return self.test_gate_service.harness_test_command_argv(command)

    def _timeout_output_to_text(self, value: str | bytes | None) -> str:
        return self.test_gate_service.timeout_output_to_text(value)

    def _require_harness_test_commands(self) -> bool:
        return self.test_gate_service.require_harness_test_commands()

    def _harness_test_commands(self, repo_dir: Path | None) -> list[str]:
        return self.test_gate_service.harness_test_commands(repo_dir)

    def _repo_has_python_files(self, repo_dir: Path) -> bool:
        return self.test_gate_service.repo_has_python_files(repo_dir)

    def _test_gate_report(
        self,
        task_id: str,
        round_id: int,
        repo_dir: Path | None,
        status: str,
        results: list[dict[str, Any]],
    ) -> str:
        return self.test_gate_service.test_gate_report(task_id, round_id, repo_dir, status, results)

    def _test_gate_evidence(self, status: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        return self.test_gate_service.test_gate_evidence(status, results)

    def _test_gate_status(self, task_id: str, round_id: int) -> str | None:
        return self.test_gate_service.status_for_round(task_id, round_id)

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
        return self.patch_gate_service.run_validation(task_id, round_id)

    def _patch_gate_policy(self):
        return self.patch_gate_service.policy()

    def _latest_merged_patch_for_round(self, task_id: str, round_id: int) -> dict[str, Any] | None:
        return self.patch_gate_service.latest_merged_patch_for_round(task_id, round_id)

    def _copy_source_for_patch_validation(self, source_repo: Path, destination: Path) -> None:
        self.materialized_repo_service.copy_source_for_patch_validation(source_repo, destination)

    def _materialized_repo_status(self, report: str) -> str:
        return self.materialized_repo_service.materialized_repo_status(report)

    def _materialized_repo_field(self, report: str, field_name: str) -> str | None:
        return self.materialized_repo_service.materialized_repo_field(report, field_name)

    def _write_materialized_success_marker(self, repo_dir: Path, task_id: str, round_id: int, patch_path: Path) -> None:
        self.materialized_repo_service.write_materialized_success_marker(repo_dir, task_id, round_id, patch_path)

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
        return self.materialized_repo_service.source_repo_for_workspace()

    def _source_repo_for_existing_project_task(self, task_id: str) -> Path | None:
        return self.materialized_repo_service.source_repo_for_existing_project_task(task_id)

    def _task_uses_existing_project_source(self, task_id: str) -> bool:
        return self.materialized_repo_service.task_uses_existing_project_source(task_id)

    def _project_context_source_repo(self, task_id: str) -> Path | None:
        return self.materialized_repo_service.project_context_source_repo(task_id)

    def _project_context_source_candidates(self, content: str) -> list[Path]:
        return self.materialized_repo_service.project_context_source_candidates(content)

    def _context_line_value(self, line: str, prefix: str) -> str | None:
        return self.materialized_repo_service.context_line_value(line, prefix)

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
        return self.materialized_repo_service.should_use_materialized_repo(role, phase)

    def _prepare_materialized_workspace_repo(self, task_id: str, role: str, phase: str, repo_dir: Path) -> None:
        self.materialized_repo_service.prepare_workspace_repo(task_id, role, phase, repo_dir)

    def _copy_ignore_for_materialized_workspace(self, directory: str, names: list[str]) -> set[str]:
        return self.materialized_repo_service.copy_ignore_for_materialized_workspace(directory, names)

    def _repo_context_metadata(self, task_id: str, role: str, phase: str) -> dict[str, Any]:
        return self.materialized_repo_service.repo_context_metadata(task_id, role, phase)

    def _materialized_root(self, task_id: str) -> Path:
        return self.materialized_repo_service.materialized_root(task_id)

    def _materialized_repo_dir(self, task_id: str, round_id: int) -> Path:
        return self.materialized_repo_service.materialized_repo_dir(task_id, round_id)

    def _latest_materialized_repo(self, task_id: str) -> Path | None:
        return self.materialized_repo_service.latest_materialized_repo(task_id)

    def _materialized_round_number(self, path: Path) -> int:
        try:
            return int(path.name.removeprefix("round_"))
        except ValueError:
            return -1

    def _latest_successful_materialized_round_from_artifacts(self, task_id: str) -> int | None:
        return self.materialized_repo_service.latest_successful_materialized_round_from_artifacts(task_id)

    def _extract_materialized_report_round(self, report: str) -> int | None:
        return self.materialized_repo_service.extract_materialized_report_round(report)

    def _materialized_success_marker_ok(self, repo_dir: Path, task_id: str, round_id: int) -> bool:
        return self.materialized_repo_service.materialized_success_marker_ok(repo_dir, task_id, round_id)

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
        self.repository.record_event(
            event_type=event.event_type,
            task_id=event.task_id,
            phase=event.phase,
            role=event.role,
            agent_id=event.agent_id,
            round_id=event.round_id,
            attempt=event.attempt,
            status=event.status,
            message=event.message,
            payload=event.data,
        )
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
        repo_dir: Path | None = None,
    ) -> list[Path]:
        return self.input_staging_service.stage(
            task_id,
            input_dir,
            role,
            phase,
            exclude_phase_id=exclude_phase_id,
            round_id=round_id,
            current_agent_id=current_agent_id,
            repo_dir=repo_dir,
        )

    def _test_target_manifest_lines(
        self,
        task_id: str,
        role: str,
        phase: str,
        round_id: int | None,
        repo_dir: Path | None,
    ) -> list[str]:
        return self.input_staging_service.test_target_manifest_lines(task_id, role, phase, round_id, repo_dir)

    def _testing_failure_context_manifest_lines(
        self,
        task_id: str,
        artifacts: list[dict[str, Any]],
        phases_by_id: dict[str, dict[str, Any]],
        role: str,
        phase: str,
        round_id: int | None,
    ) -> list[str]:
        return self.input_staging_service.testing_failure_context_manifest_lines(
            task_id, artifacts, phases_by_id, role, phase, round_id
        )

    def _failed_test_rounds_before(
        self,
        task_id: str,
        phases_by_id: dict[str, dict[str, Any]],
        round_id: int,
    ) -> list[int]:
        return self.input_staging_service.failed_test_rounds_before(task_id, phases_by_id, round_id)

    def _artifact_input_limits(self) -> dict[str, int]:
        return self.input_staging_service.artifact_input_limits()

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
        return self.input_staging_service.copy_artifact_with_budget(
            source,
            destination,
            max_file_bytes=max_file_bytes,
            remaining_total_bytes=remaining_total_bytes,
        )

    def _append_skipped_artifact_manifest(
        self,
        manifest_lines: list[str],
        index: int,
        artifact: dict[str, Any],
        source: Path,
        reason: str,
    ) -> None:
        self.input_staging_service.append_skipped_artifact_manifest(manifest_lines, index, artifact, source, reason)

    def _append_path_only_artifact_manifest(
        self,
        manifest_lines: list[str],
        index: int,
        artifact: dict[str, Any],
        source: Path,
        reason: str,
    ) -> None:
        self.input_staging_service.append_path_only_artifact_manifest(manifest_lines, index, artifact, source, reason)

    def _artifact_staging_mode(self, role: str, phase: str, artifact: dict[str, Any], source: Path) -> str:
        return self.input_staging_service.artifact_staging_mode(role, phase, artifact, source)

    def _artifact_max_file_bytes(self, configured_max_file_bytes: int, staging_mode: str) -> int:
        return self.input_staging_service.artifact_max_file_bytes(configured_max_file_bytes, staging_mode)

    def _publish_delivery(self, task_id: str, final_path: Path) -> Path:
        return self.delivery_publisher.publish_delivery(task_id, final_path)

    def delivery_success_path(self, task_id: str) -> Path | None:
        return self.delivery_publisher.delivery_success_path(task_id)

    def _delivery_project_dir(self, task_id: str, prompt: str, deliver_root: Path | None = None) -> Path:
        return self.delivery_publisher.delivery_project_dir(task_id, prompt, deliver_root)

    def _write_success_path(self, task_id: str, project_dir: Path, final_delivery: Path, usage_guide: Path | None) -> Path:
        return self.delivery_publisher.write_success_path(task_id, project_dir, final_delivery, usage_guide)

    def _record_published_artifact(self, task_id: str, artifact_type: str, path: Path) -> None:
        self.delivery_publisher.record_published_artifact(task_id, artifact_type, path)

    def _publish_supporting_artifacts(self, task_id: str, project_dir: Path) -> list[tuple[str, Path]]:
        return self.delivery_publisher.publish_supporting_artifacts(task_id, project_dir)

    def _publish_materialized_source(self, task_id: str, project_dir: Path) -> list[Path]:
        return self.delivery_publisher.publish_materialized_source(task_id, project_dir)

    def _publish_dependency_installer(self, project_dir: Path) -> list[Path]:
        return self.delivery_publisher.publish_dependency_installer(project_dir)

    def _infer_delivery_python_dependencies(self, source_dir: Path, project_dir: Path) -> list[str]:
        return self.delivery_publisher.infer_delivery_python_dependencies(source_dir, project_dir)

    def _copy_ignore_for_publish(self, directory: str, names: list[str]) -> set[str]:
        return self.delivery_publisher.copy_ignore_for_publish(directory, names)

    def _new_files_from_unified_diff(self, patch_text: str) -> dict[Path, list[str]]:
        return self.delivery_publisher.new_files_from_unified_diff(patch_text)

    def _materialized_files_from_unified_diff(
        self,
        patch_text: str,
        source_repo: Path | None,
        *,
        include_modified: bool,
    ) -> dict[Path, list[str]]:
        return self.delivery_publisher.materialized_files_from_unified_diff(
            patch_text,
            source_repo,
            include_modified=include_modified,
        )

    def _strip_diff_path(self, raw_path: str) -> Path:
        return self.delivery_publisher.strip_diff_path(raw_path)

    def _parse_old_hunk_start(self, header: str) -> int | None:
        return self.delivery_publisher.parse_old_hunk_start(header)

    def _configured_source_repo(self) -> Path | None:
        source_repo = self.config.get("system", {}).get("source_repo")
        if not source_repo:
            return None
        path = Path(str(source_repo)).expanduser().resolve()
        return path if path.exists() and path.is_dir() else None

    def _latest_patch_artifact(self, task_id: str) -> dict[str, Any] | None:
        return self.delivery_publisher.latest_patch_artifact(task_id)

    def _safe_deliver_filename(self, artifact_type: str) -> str:
        return self.delivery_publisher.safe_deliver_filename(artifact_type)

    def _is_safe_relative_path(self, path: Path) -> bool:
        return self.delivery_publisher.is_safe_relative_path(path)

    def _is_relative_to(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _slugify_project_name(self, prompt: str) -> str:
        return self.delivery_publisher.slugify_project_name(prompt)
