from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from harness.adapters.base import AgentAdapter
from harness.adapters.claude_code_adapter import REQUEST_SIZE_ERROR_PATTERNS
from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult, ArtifactRef
from harness.core.errors import TaskFailedError
from harness.core.progress import ProgressEvent


class NonRetryableAgentError(TaskFailedError):
    """Agent failure that cannot be fixed by rerunning the same prompt."""


class AgentPhaseRunner:
    def __init__(self, orchestrator: Any):
        self.orchestrator = orchestrator

    def run_role_phase(
        self,
        role: str,
        phase: str,
        round_id: int,
        required_outputs: list[str],
        user_prompt: str | None = None,
        agent_count_override: int | None = None,
    ) -> list[AgentRunResult]:
        o = self.orchestrator
        task_id = o._single_active_task_id(user_prompt)
        task = o.repository.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        user_prompt = user_prompt if user_prompt is not None else task["user_prompt"]
        agent_count = o._effective_agent_count(task_id, role, phase, agent_count_override)

        checkpoint_phase, checkpoint_results = self._recover_completed_checkpoint(
            task_id,
            phase,
            role,
            round_id,
            agent_count,
            required_outputs,
        )
        if checkpoint_phase:
            phase_id = checkpoint_phase["phase_id"]
            if checkpoint_phase["status"] == "FAILED":
                o.repository.update_phase_status(phase_id, "COMPLETED")
                o.logger.info(
                    "Recovered failed phase %s %s round=%s from completed agent runs",
                    role,
                    phase,
                    round_id,
                )
            o.logger.info("Resuming task %s: Skipping completed phase %s %s round=%s", task_id, role, phase, round_id)
            o._emit(
                ProgressEvent(
                    "phase_skipped",
                    task_id=task_id,
                    phase=phase,
                    role=role,
                    round_id=round_id,
                    status="COMPLETED",
                    message=f"Skipping completed {phase} (resuming from checkpoint)",
                )
            )
            o._emit(
                ProgressEvent(
                    "phase_completed",
                    task_id=task_id,
                    phase=phase,
                    role=role,
                    round_id=round_id,
                    status="COMPLETED",
                    message=f"{phase} recovered from checkpoint",
                    data={"artifacts": sum(len(r.artifacts) for r in checkpoint_results)},
                )
            )
            return checkpoint_results

        o.repository.update_task(task_id, status=phase, current_phase=phase, current_role=role)
        phase_id = o.repository.create_phase(task_id, phase, role, round_id)
        timeout_seconds = o.config_service.timeout_for(task_id, role)
        backend = o._backend_for(task_id, role)
        adapter = o._adapter_for_backend(backend)
        agent_ids = [f"{role}-{index + 1}" for index in range(agent_count)]
        o.logger.info("Running %s phase %s with %s agent(s)", role, phase, agent_count)
        phase_started_at = time.monotonic()
        o._emit(
            ProgressEvent(
                "phase_started",
                task_id=task_id,
                phase=phase,
                role=role,
                round_id=round_id,
                status="RUNNING",
                message=f"{phase} started with {agent_count} {role} agent(s)",
                data={"backend": backend},
            )
        )

        try:
            if o.config["policy"].get("same_role_can_run_concurrently", True) and agent_count > 1:
                results = self.run_agents_concurrently(
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
            else:
                results = [
                    self.run_agent_with_retry(
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
                    )
                    for agent_id in agent_ids
                ]
            if len(results) != agent_count:
                raise TaskFailedError(f"Only {len(results)} of {agent_count} {role} agents completed")
            o.repository.update_phase_status(phase_id, "COMPLETED")
            elapsed_seconds = round(time.monotonic() - phase_started_at, 3)
            o._emit(
                ProgressEvent(
                    "phase_completed",
                    task_id=task_id,
                    phase=phase,
                    role=role,
                    round_id=round_id,
                    status="COMPLETED",
                    message=f"{phase} completed in {elapsed_seconds}s",
                    data={"elapsed_seconds": elapsed_seconds},
                )
            )
            return results
        except Exception as exc:
            o.repository.update_phase_status(phase_id, "FAILED")
            elapsed_seconds = round(time.monotonic() - phase_started_at, 3)
            o._emit(
                ProgressEvent(
                    "phase_failed",
                    task_id=task_id,
                    phase=phase,
                    role=role,
                    round_id=round_id,
                    status="FAILED",
                    message=str(exc),
                    data={"elapsed_seconds": elapsed_seconds},
                )
            )
            raise TaskFailedError(f"Role phase failed: role={role} phase={phase}: {exc}") from exc

    def _recover_completed_checkpoint(
        self,
        task_id: str,
        phase: str,
        role: str,
        round_id: int,
        agent_count: int,
        required_outputs: list[str],
    ) -> tuple[dict[str, Any] | None, list[AgentRunResult]]:
        existing_phases = self.orchestrator.repository.list_phases(task_id)
        checkpoint_candidates = [
            p
            for p in existing_phases
            if p["phase_type"] == phase
            and p["role"] == role
            and p["round_id"] == round_id
            and p["status"] in {"COMPLETED", "FAILED"}
        ]
        for candidate in reversed(checkpoint_candidates):
            candidate_results = self.recover_phase_results(task_id, candidate["phase_id"])
            recoverable = (
                len({result.agent_id for result in candidate_results}) >= agent_count
                and self.recovered_results_have_required_outputs(candidate_results, required_outputs)
            )
            if recoverable:
                return candidate, candidate_results
        return None, []

    def recover_phase_results(self, task_id: str, phase_id: str) -> list[AgentRunResult]:
        o = self.orchestrator
        runs = o.repository.list_agent_runs(task_id)
        phase_runs = [run for run in runs if run["phase_id"] == phase_id and run["status"] == "COMPLETED"]
        artifacts = o.repository.list_artifacts(task_id)
        results: list[AgentRunResult] = []
        for run in phase_runs:
            run_artifacts = [
                ArtifactRef(
                    artifact_id=artifact["artifact_id"],
                    task_id=artifact["task_id"],
                    phase_id=artifact["phase_id"],
                    role=artifact["role"],
                    agent_id=artifact["agent_id"],
                    artifact_type=artifact["artifact_type"],
                    path=Path(artifact["path"]),
                    version=artifact["version"],
                    hash=artifact["hash"],
                )
                for artifact in artifacts
                if artifact["phase_id"] == phase_id and artifact["agent_id"] == run["agent_id"]
            ]
            results.append(
                AgentRunResult(
                    task_id=task_id,
                    phase_id=phase_id,
                    role=run["role"],
                    agent_id=run["agent_id"],
                    status="COMPLETED",
                    exit_code=0,
                    artifacts=run_artifacts,
                    validation_ok=True,
                )
            )
        return results

    def recovered_results_have_required_outputs(self, results: list[AgentRunResult], required_outputs: list[str]) -> bool:
        for result in results:
            artifacts_by_type = {artifact.artifact_type: artifact.path for artifact in result.artifacts}
            for output_name in required_outputs:
                path = artifacts_by_type.get(output_name)
                if not path or not path.exists() or not path.is_file() or path.stat().st_size == 0:
                    return False
            delivery_path = artifacts_by_type.get("delivery.md")
            if delivery_path and self.orchestrator.validator.parse_delivery_status(delivery_path) != "success":
                return False
        return bool(results)

    def run_agents_concurrently(
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
        max_retry = int(self.orchestrator.config["limits"]["max_agent_retry"])
        per_attempt_grace_seconds = 10
        phase_timeout_seconds = None
        if timeout_seconds > 0:
            phase_timeout_seconds = (max_retry + 1) * (timeout_seconds + per_attempt_grace_seconds)
        cancel_event = threading.Event()
        executor = ThreadPoolExecutor(max_workers=len(agent_ids))
        futures = {
            executor.submit(
                self.run_agent_with_retry,
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
            ): agent_id
            for agent_id in agent_ids
        }
        try:
            done, unfinished = wait(futures, timeout=phase_timeout_seconds)
            if unfinished:
                cancel_event.set()
                unfinished_agents = ", ".join(sorted(futures[future] for future in unfinished))
                for future in unfinished:
                    future.cancel()
                raise TaskFailedError(
                    f"{len(unfinished)} of {len(futures)} {role} agent(s) did not finish within "
                    f"{phase_timeout_seconds}s: {unfinished_agents}"
                )
            return [future.result() for future in done]
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def run_agent_with_retry(
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
        cancel_event: threading.Event | None = None,
    ) -> AgentRunResult:
        o = self.orchestrator
        max_retry = int(o.config["limits"]["max_agent_retry"])
        last_result: AgentRunResult | None = None
        last_error_message: str | None = None
        for attempt in range(max_retry + 1):
            if cancel_event and cancel_event.is_set():
                raise TaskFailedError(f"Agent {agent_id} cancelled because the phase timed out")
            attempt_started_at = time.monotonic()
            run_id = o.repository.create_agent_run(task_id, phase_id, role, agent_id, attempt)
            workspace = o.workspace_manager.create_workspace(
                task_id,
                phase_id,
                role,
                agent_id,
                round_id,
                attempt,
                source_repo=o._source_repo_for_workspace(),
            )
            o._prepare_materialized_workspace_repo(task_id, role, phase, workspace.repo_dir)
            input_artifacts = o._stage_input_artifacts(
                task_id,
                workspace.input_dir,
                role,
                phase,
                exclude_phase_id=phase_id,
                round_id=round_id,
                current_agent_id=agent_id,
            )
            task_for_metadata = o.repository.get_task(task_id) or {"task_id": task_id, "user_prompt": user_prompt}
            metadata = o._context_metadata(task_for_metadata, role, phase)
            metadata.update(o._repo_context_metadata(task_id, role, phase))
            context = AgentRunContext(
                task_id=task_id,
                phase_id=phase_id,
                phase=phase,
                role=role,
                agent_id=agent_id,
                round_id=round_id,
                user_prompt=user_prompt,
                role_instruction=o.role_instructions.get(role, ""),
                workspace_dir=workspace.workspace_dir,
                repo_dir=workspace.repo_dir,
                input_dir=workspace.input_dir,
                output_dir=workspace.output_dir,
                log_dir=workspace.log_dir,
                input_artifacts=input_artifacts,
                required_outputs=required_outputs,
                timeout_seconds=timeout_seconds,
                config=o.config_service.config_for_task(task_id),
                metadata=metadata,
            )
            context.log_dir.mkdir(parents=True, exist_ok=True)
            (context.log_dir / "prompt.md").write_text(o.prompt_builder.build(context), encoding="utf-8")
            o._emit(
                ProgressEvent(
                    "agent_started",
                    task_id=task_id,
                    phase=phase,
                    role=role,
                    agent_id=agent_id,
                    round_id=round_id,
                    attempt=attempt,
                    status="RUNNING",
                    message=f"{agent_id} attempt {attempt + 1} invoking {adapter.__class__.__name__}",
                    data={
                        "backend": o._backend_for(task_id, role),
                        "workspace": str(context.workspace_dir),
                        "output": str(context.output_dir),
                        "logs": str(context.log_dir),
                    },
                )
            )
            try:
                result = self.run_adapter_with_heartbeat(adapter, context, attempt)
                if cancel_event and cancel_event.is_set():
                    message = "Phase timed out before this agent result was accepted; ignoring late result"
                    o.repository.update_agent_run_status(run_id, "TIMEOUT", message)
                    raise TaskFailedError(message)
                ok, errors = o.validator.validate_required_outputs(workspace.output_dir, required_outputs)
                delivery_status = o.validator.parse_delivery_status(workspace.output_dir / "delivery.md")
                result.validation_ok = ok
                result.validation_errors = errors
                if result.status == "COMPLETED" and result.exit_code == 0 and ok:
                    result.artifacts = o.artifact_manager.collect_output_dir(
                        task_id, phase_id, role, agent_id, workspace.output_dir
                    )
                    o.repository.update_agent_run_status(run_id, "COMPLETED")
                    elapsed_seconds = round(time.monotonic() - attempt_started_at, 3)
                    o._emit(
                        ProgressEvent(
                            "agent_completed",
                            task_id=task_id,
                            phase=phase,
                            role=role,
                            agent_id=agent_id,
                            round_id=round_id,
                            attempt=attempt,
                            status="COMPLETED",
                            message=f"{agent_id} completed in {elapsed_seconds}s",
                            data={
                                "artifacts": len(result.artifacts),
                                "delivery_status": delivery_status or "-",
                                "elapsed_seconds": elapsed_seconds,
                            },
                        )
                    )
                    return result
                status = "OUTPUT_INVALID" if not ok else "FAILED"
                message = "; ".join(errors) if errors else f"Agent exit_code={result.exit_code} status={result.status}"
                terminal_failure = self.is_request_size_failure(result, context, message)
                if terminal_failure:
                    status = "FAILED"
                    message = self.request_size_failure_message(context)
                last_error_message = message
                o.repository.update_agent_run_status(run_id, status, message)
                elapsed_seconds = round(time.monotonic() - attempt_started_at, 3)
                diagnostics_path = context.log_dir / "request_diagnostics.md"
                event_data = {
                    "logs": str(context.log_dir),
                    "delivery_status": delivery_status or "-",
                    "elapsed_seconds": elapsed_seconds,
                }
                if diagnostics_path.exists():
                    event_data["diagnostics"] = str(diagnostics_path)
                o._emit(
                    ProgressEvent(
                        "agent_failed" if terminal_failure else "agent_retryable_failure",
                        task_id=task_id,
                        phase=phase,
                        role=role,
                        agent_id=agent_id,
                        round_id=round_id,
                        attempt=attempt,
                        status=status,
                        message=message,
                        data=event_data,
                    )
                )
                if terminal_failure:
                    raise NonRetryableAgentError(message)
                last_result = result
            except NonRetryableAgentError:
                raise
            except Exception as exc:
                last_error_message = str(exc)
                failure_status = "TIMEOUT" if cancel_event and cancel_event.is_set() else "FAILED"
                terminal_failure = self.text_contains_request_size_error(str(exc)) or self.logs_contain_request_size_error(
                    context.log_dir
                )
                status_message = str(exc)
                if terminal_failure:
                    last_error_message = self.request_size_failure_message(context)
                    status_message = last_error_message
                o.repository.update_agent_run_status(run_id, failure_status, status_message)
                elapsed_seconds = round(time.monotonic() - attempt_started_at, 3)
                diagnostics_path = context.log_dir / "request_diagnostics.md"
                event_data = {"logs": str(context.log_dir), "elapsed_seconds": elapsed_seconds}
                if diagnostics_path.exists():
                    event_data["diagnostics"] = str(diagnostics_path)
                o._emit(
                    ProgressEvent(
                        "agent_failed" if terminal_failure else "agent_retryable_failure",
                        task_id=task_id,
                        phase=phase,
                        role=role,
                        agent_id=agent_id,
                        round_id=round_id,
                        attempt=attempt,
                        status=failure_status,
                        message=status_message,
                        data=event_data,
                    )
                )
                if terminal_failure:
                    raise NonRetryableAgentError(last_error_message) from exc
                last_result = AgentRunResult(task_id, phase_id, role, agent_id, "FAILED", exit_code=1)
            if attempt >= max_retry:
                break
        if last_result:
            details = last_result.validation_errors or ([last_error_message] if last_error_message else [])
            raise TaskFailedError(f"Agent {agent_id} failed after {max_retry + 1} attempt(s): {details}")
        raise TaskFailedError(f"Agent {agent_id} failed before producing a result")

    def is_request_size_failure(self, result: AgentRunResult, context: AgentRunContext, message: str) -> bool:
        if self.text_contains_request_size_error(message):
            return True
        texts = []
        for path in (result.stdout_path, result.stderr_path, context.log_dir / "request_diagnostics.md"):
            if path and path.exists():
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        return self.text_contains_request_size_error("\n".join(texts))

    def logs_contain_request_size_error(self, log_dir: Path) -> bool:
        texts = []
        for name in ("stdout.log", "stderr.log", "request_diagnostics.md"):
            path = log_dir / name
            if path.exists():
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        return self.text_contains_request_size_error("\n".join(texts))

    def text_contains_request_size_error(self, text: str) -> bool:
        return any(pattern in text for pattern in REQUEST_SIZE_ERROR_PATTERNS)

    def request_size_failure_message(self, context: AgentRunContext) -> str:
        diagnostics_path = context.log_dir / "request_diagnostics.md"
        return (
            "Agent request exceeded the model context/request-size budget; not retrying the same prompt. "
            f"Lower claude.max_output_tokens for role={context.role}, reduce staged artifact input, or use a larger model window. "
            f"Diagnostics: {diagnostics_path}"
        )

    def run_adapter_with_heartbeat(self, adapter: AgentAdapter, context: AgentRunContext, attempt: int) -> AgentRunResult:
        o = self.orchestrator
        interval = float(o.config.get("heartbeat", {}).get("interval_seconds", 60))
        if interval <= 0:
            return adapter.run(context)
        stop_event = threading.Event()
        started_at = time.monotonic()

        def beat() -> None:
            while not stop_event.wait(interval):
                elapsed_seconds = int(time.monotonic() - started_at)
                o._emit(
                    ProgressEvent(
                        "agent_heartbeat",
                        task_id=context.task_id,
                        phase=context.phase,
                        role=context.role,
                        agent_id=context.agent_id,
                        round_id=context.round_id,
                        attempt=attempt,
                        status="RUNNING",
                        message=f"{context.agent_id} still running after {elapsed_seconds}s",
                        data={
                            "backend": o._backend_for(context.task_id, context.role),
                            "workspace": str(context.workspace_dir),
                            "logs": str(context.log_dir),
                            "elapsed_seconds": elapsed_seconds,
                        },
                    )
                )

        heartbeat_thread = threading.Thread(target=beat, name=f"heartbeat-{context.agent_id}", daemon=True)
        heartbeat_thread.start()
        try:
            return adapter.run(context)
        finally:
            stop_event.set()
            heartbeat_thread.join(timeout=1)
