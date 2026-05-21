from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from harness.adapters.base import AgentAdapter
from harness.adapters.claude_code_adapter import ClaudeCodeAdapter
from harness.adapters.codex_cli_adapter import CodexCLIAdapter
from harness.adapters.headless_cli_adapter import HeadlessCLIAdapter
from harness.adapters.mock_adapter import MockAgentAdapter
from harness.agents.result import AgentRunResult
from harness.app.bootstrap import build_orchestrator_services
from harness.artifacts.visibility import ARTIFACT_VISIBILITY_RULES
from harness.config.loader import load_config
from harness.contracts.role_contracts import required_outputs_for, role_instruction_for as contract_role_instruction_for
from harness.core.errors import TaskFailedError
from harness.core.progress import ProgressCallback, ProgressEvent
from harness.core.state_machine import (
    COMPLETED,
    CREATED,
    DELIVERY,
    EXECUTION,
    FAILED,
    FIXING,
    MISC_RESPONSE,
    PATCH_MERGE,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REVIEW_FIXING,
    REVIEWING,
    RUNNING,
)
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, MISC, NEW_PROJECT, normalize_workflow_type
from harness.runtime.resolver import RuntimeResolver

if TYPE_CHECKING:
    from harness.artifacts.manager import ArtifactManager
    from harness.state.repository import StateRepository
    from harness.workspace.manager import WorkspaceManager


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
        self._active_task_id: str | None = None
        self._active_workflow_type: str | None = None
        self._active_task_resume_status: str | None = None
        self.progress_callback = progress_callback
        self.fix_round_limit_callback = fix_round_limit_callback
        services = build_orchestrator_services(
            self,
            config=self.config,
            repository=repository,
            workspace_manager=workspace_manager,
            artifact_manager=artifact_manager,
        )
        self.repository = services.repository
        self.config_service = services.config_service
        self.workspace_manager = services.workspace_manager
        self.artifact_manager = services.artifact_manager
        self.artifact_visibility = services.artifact_visibility
        self.validator = services.validator
        self.event_store = services.event_store
        self.artifact_plane = services.artifact_plane
        self.communicator = services.communicator
        self.backend_health = services.backend_health
        self.scheduler = services.scheduler
        self.prompt_builder = services.prompt_builder
        self.materialized_repo_service = services.materialized_repo_service
        self.test_gate_service = services.test_gate_service
        self.runtime_readiness_gate_service = services.runtime_readiness_gate_service
        self.final_validation_gate_service = services.final_validation_gate_service
        self.patch_gate_service = services.patch_gate_service
        self.input_staging_service = services.input_staging_service
        self.agent_runner = services.agent_runner
        self.delivery_publisher = services.delivery_publisher
        self.workflow_engine = services.workflow_engine
        self.logger = services.logger

    def create_task(self, user_prompt: str, workflow_type: str | None = None) -> str:
        task_id = self.repository.create_task(user_prompt, CREATED, workflow_type=workflow_type)
        self._emit(ProgressEvent("task_created", task_id=task_id, status=CREATED, message="Task created"))
        return task_id

    def attach_project_context(self, task_id: str, content: str) -> None:
        if content.strip():
            self.artifact_manager.create_text_artifact(task_id, "project_context.md", content)

    def record_task_classification(self, task_id: str, classification: dict[str, Any]) -> None:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        try:
            configuration = json.loads(task["configuration"] or "{}")
        except json.JSONDecodeError:
            configuration = {}
        if not isinstance(configuration, dict):
            configuration = {}
        configuration["classification"] = classification
        self.repository.update_task_configuration(
            task_id,
            json.dumps(configuration, ensure_ascii=False, sort_keys=True),
        )

    def run_task(self, task_id: str, workflow_type: str | None = None, user_prompt_override: str | None = None) -> Path:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        workflow_type = normalize_workflow_type(workflow_type or task.get("workflow_type") or NEW_PROJECT)

        if not task.get("workflow_type"):
            self.repository.set_task_workflow_type(task_id, workflow_type)

        raw_user_prompt = user_prompt_override or str(task["user_prompt"])
        if user_prompt_override and user_prompt_override.strip() and user_prompt_override.strip() != str(task["user_prompt"]).strip():
            self.repository.append_task_prompt_turn(task_id, user_prompt_override)
        user_prompt = self._workflow_prompt(raw_user_prompt, workflow_type)
        self._active_task_id = task_id
        self._active_workflow_type = workflow_type
        self._active_task_resume_status = str(task.get("status") or "")
        if str(task.get("status") or "") == COMPLETED:
            self.repository.reopen_task_for_followup(task_id)
        else:
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
        self.record_resolved_runtime(task_id)
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

    def _run_bugfix_flow(self, task_id: str, user_prompt: str) -> Path:
        return self.workflow_engine.run_bugfix_flow(task_id, user_prompt)

    def _run_planning_block(self, task_id: str, user_prompt: str) -> None:
        self.workflow_engine.run_planning_block(task_id, user_prompt)

    def _run_execution_test_loop(self, task_id: str, user_prompt: str) -> None:
        self.workflow_engine.run_execution_test_loop(task_id, user_prompt)

    def _max_test_fix_rounds(self) -> int | None:
        return self.workflow_engine.max_test_fix_rounds()

    def _run_review_loop(self, task_id: str, user_prompt: str) -> None:
        self.workflow_engine.run_review_loop(task_id, user_prompt)

    def _run_regression_test_fix_loop(self, task_id: str, user_prompt: str, review_round_id: int, merge_ok: bool) -> None:
        self.workflow_engine.run_regression_test_fix_loop(task_id, user_prompt, review_round_id, merge_ok)

    def run_role_phase(
        self,
        role: str,
        phase: str,
        round_id: int,
        required_outputs: list[str],
        user_prompt: str | None = None,
        agent_count_override: int | None = None,
        phase_scope: dict[str, int | str | None] | None = None,
        retry_feedback: list[str] | None = None,
    ) -> list[AgentRunResult]:
        return self.agent_runner.run_role_phase(
            role,
            phase,
            round_id,
            required_outputs,
            user_prompt,
            agent_count_override,
            phase_scope,
            retry_feedback,
        )

    def is_failed_resume(self, task_id: str) -> bool:
        return self._active_task_id == task_id and self._active_task_resume_status == FAILED

    def emit_progress(self, event: ProgressEvent) -> None:
        self._emit(event)

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

    def markdown_field(self, content: str, field_name: str) -> str | None:
        prefix = f"{field_name}:"
        for line in content.splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
        return None

    def _markdown_field(self, content: str, field_name: str) -> str | None:
        return self.markdown_field(content, field_name)

    def run_harness_test_gate(self, task_id: str, round_id: int) -> bool:
        self.test_gate_service.latest_materialized_repo = self._latest_materialized_repo
        return self.test_gate_service.run(task_id, round_id)

    def _run_harness_test_gate(self, task_id: str, round_id: int) -> bool:
        return self.run_harness_test_gate(task_id, round_id)

    def run_runtime_readiness_gate(self, task_id: str, round_id: int) -> bool:
        self.test_gate_service.latest_materialized_repo = self._latest_materialized_repo
        return self.runtime_readiness_gate_service.run(task_id, round_id)

    def _run_runtime_readiness_gate(self, task_id: str, round_id: int) -> bool:
        return self.run_runtime_readiness_gate(task_id, round_id)

    def run_final_validation_gate(self, task_id: str, round_id: int):
        self.final_validation_gate_service.latest_materialized_repo = self._latest_materialized_repo
        return self.final_validation_gate_service.run(task_id, round_id)

    def _run_final_validation_gate(self, task_id: str, round_id: int):
        return self.run_final_validation_gate(task_id, round_id)

    def _harness_test_command_argv(self, command: str) -> list[str]:
        return self.test_gate_service.harness_test_command_argv(command)

    def _timeout_output_to_text(self, value: str | bytes | None) -> str:
        return self.test_gate_service.command_runner.decode_timeout_stream(value)

    def _require_harness_test_commands(self) -> bool:
        return self.test_gate_service.require_harness_test_commands()

    def _harness_test_commands(self, repo_dir: Path | None):
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
        return self.test_gate_service.test_gate_report("Harness Test Gate", task_id, round_id, repo_dir, status, results)

    def _test_gate_evidence(self, status: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        return self.test_gate_service.test_gate_evidence(status, results)

    def _test_gate_status(self, task_id: str, round_id: int) -> str | None:
        return self.test_gate_service.status_for_round(task_id, round_id)

    def test_gate_failure_type_for_round(self, task_id: str, round_id: int) -> str | None:
        return self.test_gate_service.failure_type_for_round(task_id, round_id)

    def _test_gate_failure_type_for_round(self, task_id: str, round_id: int) -> str | None:
        return self.test_gate_failure_type_for_round(task_id, round_id)

    def run_patch_merge(self, task_id: str, round_id: int, user_prompt: str) -> bool:
        if not self.patch_gate_service.latest_merged_patch_for_round(task_id, round_id):
            if self.patch_gate_service.try_accept_noop_candidate_patch(task_id, round_id):
                return True
            if self.patch_gate_service.try_skip_noop_candidate_patch(task_id, round_id):
                return False
            if not self.patch_gate_service.try_deterministic_single_candidate_merge(task_id, round_id):
                self.run_role_phase(
                    "executor",
                    PATCH_MERGE,
                    round_id,
                    required_outputs_for("executor", PATCH_MERGE),
                    user_prompt,
                    agent_count_override=1,
                )
        return self._run_patch_validation(task_id, round_id)

    def _run_patch_merge(self, task_id: str, round_id: int, user_prompt: str) -> bool:
        return self.run_patch_merge(task_id, round_id, user_prompt)

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

    def _write_materialized_success_marker(self, repo_dir: Path, task_id: str, round_id: int, patch_path: Path, **kwargs: Any) -> None:
        self.materialized_repo_service.write_materialized_success_marker(repo_dir, task_id, round_id, patch_path, **kwargs)

    def backend_for(self, task_id: str | None, role: str) -> str:
        return self.config_service.backend_for(task_id, role)

    def _backend_for(self, task_id: str | None, role: str) -> str:
        return self.backend_for(task_id, role)

    def adapter_for_backend(self, backend: str) -> AgentAdapter:
        return self._adapter_for_backend(backend)

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

    def record_resolved_runtime(self, task_id: str) -> None:
        config = self.config_service.config_for_task(task_id)
        spec = RuntimeResolver(config).resolve()
        payload = {
            "schema_version": "resolved_runtime.v1",
            "runtime": {
                "mode": spec.mode,
                "image": spec.image,
                "workdir": spec.workdir,
                "network": spec.network,
                "cache_root": str(spec.cache_root) if spec.cache_root else "",
                "env_allowlist": list(spec.env_allowlist),
            },
            "source": "harness",
        }
        self.artifact_manager.create_text_artifact(
            task_id,
            "resolved_runtime.json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            role="orchestrator",
            agent_id="runtime-resolver",
        )

    def source_repo_for_workspace(self) -> Path | None:
        return self.materialized_repo_service.source_repo_for_workspace()

    def _source_repo_for_workspace(self) -> Path | None:
        return self.source_repo_for_workspace()

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

    def effective_agent_count(self, task_id: str, role: str, phase: str, agent_count_override: int | None = None) -> int:
        if role == "executor" and phase in SINGLE_EXECUTOR_FIX_PHASES:
            # Intentional hard rule for future AI maintainers: fix phases use one executor.
            # Multiple fix executors produce competing patches for the same defect, which
            # increases merge conflicts, context size, and risk of broad accidental changes.
            return 1
        workflow_type = self._active_workflow_type
        if not workflow_type and task_id:
            task = self.repository.get_task(task_id)
            workflow_type = str(task.get("workflow_type") or "") if task else None
        if role == "planner" and workflow_type == BUGFIX:
            if agent_count_override is not None:
                return agent_count_override
            return 2
        if role == "planner" and workflow_type == FEATURE_CHANGE:
            configured = agent_count_override if agent_count_override is not None else self.config_service.role_count(task_id, role)
            return min(configured, 2)
        if agent_count_override is not None:
            return agent_count_override
        return self.config_service.role_count(task_id, role)

    def _effective_agent_count(self, task_id: str, role: str, phase: str, agent_count_override: int | None = None) -> int:
        return self.effective_agent_count(task_id, role, phase, agent_count_override)

    def _should_use_materialized_repo(self, role: str, phase: str) -> bool:
        return self.materialized_repo_service.should_use_materialized_repo(role, phase)

    def prepare_materialized_workspace_repo(self, task_id: str, role: str, phase: str, repo_dir: Path) -> None:
        self.materialized_repo_service.prepare_workspace_repo(task_id, role, phase, repo_dir)

    def _prepare_materialized_workspace_repo(self, task_id: str, role: str, phase: str, repo_dir: Path) -> None:
        self.prepare_materialized_workspace_repo(task_id, role, phase, repo_dir)

    def _copy_ignore_for_materialized_workspace(self, directory: str, names: list[str]) -> set[str]:
        return self.materialized_repo_service.copy_ignore_for_materialized_workspace(directory, names)

    def repo_context_metadata(self, task_id: str, role: str, phase: str) -> dict[str, Any]:
        return self.materialized_repo_service.repo_context_metadata(task_id, role, phase)

    def _repo_context_metadata(self, task_id: str, role: str, phase: str) -> dict[str, Any]:
        return self.repo_context_metadata(task_id, role, phase)

    def _materialized_root(self, task_id: str) -> Path:
        return self.materialized_repo_service.materialized_root(task_id)

    def _materialized_repo_dir(self, task_id: str, round_id: int) -> Path:
        return self.materialized_repo_service.materialized_repo_dir(task_id, round_id)

    def _latest_materialized_repo(self, task_id: str) -> Path | None:
        return self.materialized_repo_service.latest_materialized_repo(task_id)

    def _latest_cumulative_patch(self, task_id: str) -> Path | None:
        return self.materialized_repo_service.latest_cumulative_patch(task_id)

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

    def active_task_id(self) -> str | None:
        return self._active_task_id

    def active_workflow_type(self) -> str | None:
        return self._active_workflow_type

    def single_active_task_id(self, user_prompt: str | None) -> str:
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

    def _single_active_task_id(self, user_prompt: str | None) -> str:
        return self.single_active_task_id(user_prompt)

    def _emit(self, event: ProgressEvent) -> None:
        trace_id = event.trace_id or event.task_id
        span_id = event.span_id or self._event_span_id(event)
        parent_span_id = event.parent_span_id or self._event_parent_span_id(event)
        enriched = replace(event, trace_id=trace_id, span_id=span_id, parent_span_id=parent_span_id)
        self.repository.record_event(
            event_type=enriched.event_type,
            task_id=enriched.task_id,
            phase=enriched.phase,
            role=enriched.role,
            agent_id=enriched.agent_id,
            round_id=enriched.round_id,
            attempt=enriched.attempt,
            status=enriched.status,
            message=enriched.message,
            trace_id=enriched.trace_id,
            span_id=enriched.span_id,
            parent_span_id=enriched.parent_span_id,
            payload=enriched.data,
        )
        if self.progress_callback:
            self.progress_callback(enriched)

    def _event_span_id(self, event: ProgressEvent) -> str | None:
        if not event.task_id:
            return None
        parts = [event.event_type]
        if event.phase:
            parts.append(str(event.phase))
        if event.role:
            parts.append(str(event.role))
        if event.agent_id:
            parts.append(str(event.agent_id))
        if event.round_id is not None:
            parts.append(f"round-{event.round_id}")
        if event.attempt is not None:
            parts.append(f"attempt-{event.attempt}")
        return ":".join(parts)

    def _event_parent_span_id(self, event: ProgressEvent) -> str | None:
        if not event.task_id:
            return None
        if event.agent_id and event.phase:
            parts = ["phase", str(event.phase)]
            if event.round_id is not None:
                parts.append(f"round-{event.round_id}")
            return ":".join(parts)
        if event.phase:
            return f"task:{event.task_id}"
        return None

    def context_metadata(self, task: dict[str, Any], role: str, phase: str) -> dict[str, Any]:
        metadata = {"workflow_type": task.get("workflow_type") or self._active_workflow_type or NEW_PROJECT}
        if role != "communicator" or phase != DELIVERY:
            return metadata
        task_id = str(task["task_id"])
        expected_success_path = self._delivery_project_dir(task_id, str(task["user_prompt"]))
        metadata.update({
            "expected_success_path": str(expected_success_path),
            "expected_final_delivery": str(expected_success_path / "final_delivery.json"),
            "expected_usage_guide": str(expected_success_path / "usage_guide.md"),
            "expected_artifacts_manifest": str(expected_success_path / "artifacts_manifest.md"),
            "publish_timing": "Harness will publish these files after the communicator role succeeds.",
        })
        return metadata

    def _context_metadata(self, task: dict[str, Any], role: str, phase: str) -> dict[str, Any]:
        return self.context_metadata(task, role, phase)

    def role_instruction_for(self, role: str) -> str:
        return contract_role_instruction_for(role, self._active_workflow_type or NEW_PROJECT)

    def _workflow_prompt(self, user_prompt: str, workflow_type: str) -> str:
        if workflow_type == BUGFIX:
            instruction = (
                "Workflow classification: bugfix.\n"
                "Use the repair workflow. Preserve existing behavior except where needed to fix the reported issue. "
                "Start with two focused bugfix planners: one must localize the root cause and minimal repair, and "
                "the other must define runnable validation, regression risks, and acceptance evidence. Keep the "
                "change scope minimal, produce fix artifacts, and rely on tester and reviewer artifacts to decide "
                "whether the fix is complete."
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

    def stage_input_artifacts(
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
        return self.stage_input_artifacts(
            task_id,
            input_dir,
            role,
            phase,
            exclude_phase_id=exclude_phase_id,
            round_id=round_id,
            current_agent_id=current_agent_id,
            repo_dir=repo_dir,
        )

    def _artifact_input_limits(self, role: str | None = None, phase: str | None = None) -> dict[str, Any]:
        return self.input_staging_service.artifact_input_limits(role, phase)

    def positive_int(self, value: Any, default: int, field_name: str) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer > 0, got {value!r}") from exc
        if parsed <= 0:
            raise ValueError(f"{field_name} must be an integer > 0, got {parsed}")
        return parsed

    def _positive_int(self, value: Any, default: int, field_name: str) -> int:
        return self.positive_int(value, default, field_name)

    def _artifact_staging_mode(
        self,
        role: str,
        phase: str,
        artifact: dict[str, Any],
        source: Path,
        large_artifact_mode: str = "auto",
    ) -> str:
        return self.input_staging_service.artifact_staging_mode(
            role,
            phase,
            artifact,
            source,
            large_artifact_mode=large_artifact_mode,
        )

    def latest_final_delivery(self, task_id: str) -> Path | None:
        return self.communicator.latest_final_delivery(task_id)

    def publish_delivery(self, task_id: str, final_path: Path) -> Path:
        return self.delivery_publisher.publish_delivery(task_id, final_path)

    def _publish_delivery(self, task_id: str, final_path: Path) -> Path:
        return self.publish_delivery(task_id, final_path)

    def delivery_success_path(self, task_id: str) -> Path | None:
        return self.delivery_publisher.delivery_success_path(task_id)

    def _delivery_project_dir(self, task_id: str, prompt: str, deliver_root: Path | None = None) -> Path:
        return self.delivery_publisher.delivery_project_dir(task_id, prompt, deliver_root)

    def _publish_dependency_installer(self, project_dir: Path) -> list[Path]:
        return self.delivery_publisher.publish_dependency_installer(project_dir)

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
