from __future__ import annotations

import json
import shutil
import threading
import time
import re
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from harness.adapters.base import AgentAdapter
from harness.adapters.claude_code_adapter import ClaudeCodeAdapter
from harness.adapters.codex_cli_adapter import CodexCLIAdapter
from harness.adapters.mock_adapter import MockAgentAdapter
from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult, ArtifactRef
from harness.artifacts.hashing import sha256_file
from harness.artifacts.manager import ArtifactManager
from harness.artifacts.schemas import required_outputs_for
from harness.artifacts.validator import ArtifactValidator
from harness.communication.communicator import Communicator
from harness.config.loader import load_config
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
    PLAN_JUDGEMENT,
    PLAN_REVIEW,
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
from harness.prompts.builder import PromptBuilder
from harness.state.db import StateDB
from harness.state.repository import StateRepository
from harness.workspace.manager import WorkspaceManager


ROLE_INSTRUCTIONS = {
    "planner": (
        "Create planning artifacts only. Analyze the request, existing artifacts, assumptions, risks, "
        "compatibility constraints, and an actionable task breakdown. Do not modify source files. "
        "Your delivery.md status must be 'success' if you produced a complete plan, even if you identify high risks."
    ),
    "executor": (
        "Create the artifacts required by the current executor phase. For implementation and fix phases, "
        "express code changes as unified diff files and supporting notes. For miscellaneous response phases, "
        "answer the request without modifying project files. Do not decide workflow progression or communicate "
        "with the user outside required artifacts. Your delivery.md status must be 'success' if you produced "
        "the required files, regardless of the implementation complexity."
    ),
    "tester": (
        "Evaluate executor artifacts and available repository state. Produce build, test, and bug reports "
        "with an explicit pass/fail assessment and reproducible evidence. "
        "IMPORTANT: Your delivery.md status must be 'success' as long as you have completed the evaluation and "
        "produced the reports, even if the test results themselves are 'fail' or you find critical bugs. "
        "The 'failed' status in delivery.md is only for when you are unable to complete the testing task itself."
    ),
    "reviewer": (
        "Review executor and tester artifacts for correctness, scope control, regressions, maintainability, "
        "and missing validation. Produce review findings only. Your delivery.md status must be 'success' if "
        "you completed the review, regardless of whether you approve the changes or require major revisions."
    ),
    "judge": (
        "Make the phase decision from collected artifacts only. Produce a strict machine-readable decision "
        "and a concise rationale. Do not create implementation changes. Your delivery.md status must be 'success' "
        "if you rendered a clear decision."
    ),
    "communicator": (
        "Create the final delivery artifact only. Summarize outcome, status, produced artifacts, residual "
        "risks, and next steps using the accepted artifact set. Your delivery.md status must be 'success' "
        "if the final delivery documentation is complete."
    ),
}


class Orchestrator:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        repository: StateRepository | None = None,
        workspace_manager: WorkspaceManager | None = None,
        artifact_manager: ArtifactManager | None = None,
        progress_callback: ProgressCallback | None = None,
    ):
        self.config = config or load_config()
        system = self.config["system"]
        self.repository = repository or StateRepository(StateDB(system["state_db"]))
        self.workspace_manager = workspace_manager or WorkspaceManager(system["workspace_root"])
        self.artifact_manager = artifact_manager or ArtifactManager(system["artifact_root"], self.repository)
        self.validator = ArtifactValidator()
        self.communicator = Communicator(self.repository)
        self.judge = MockJudge()
        self.prompt_builder = PromptBuilder()
        self.logger = get_logger(__name__)
        self._active_task_id: str | None = None
        self._active_workflow_type: str | None = None
        self.progress_callback = progress_callback

    def create_task(self, user_prompt: str, workflow_type: str | None = None) -> str:
        task_id = self.repository.create_task(user_prompt, CREATED, workflow_type=workflow_type)
        self._emit(ProgressEvent("task_created", task_id=task_id, status=CREATED, message="Task created"))
        return task_id

    def attach_project_context(self, task_id: str, content: str) -> None:
        if content.strip():
            self.artifact_manager.create_text_artifact(task_id, "project_context.md", content)

    def run_task(self, task_id: str, workflow_type: str = NEW_PROJECT) -> Path:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        workflow_type = normalize_workflow_type(workflow_type)
        
        # Persist workflow_type if not already set
        if not task.get("workflow_type"):
            with self.repository._lock, self.repository.db.connect() as conn:
                conn.execute("UPDATE tasks SET workflow_type = ? WHERE task_id = ?", (workflow_type, task_id))
        
        user_prompt = self._workflow_prompt(task["user_prompt"], workflow_type)
        self._active_task_id = task_id
        self._active_workflow_type = workflow_type
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
            if workflow_type == BUGFIX:
                final_path = self._run_bugfix_flow(task_id, user_prompt)
            elif workflow_type == FEATURE_CHANGE:
                final_path = self._run_feature_change_flow(task_id, user_prompt)
            elif workflow_type == MISC:
                final_path = self._run_misc_flow(task_id, user_prompt)
            else:
                final_path = self._run_new_project_flow(task_id, user_prompt)
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

    def _run_new_project_flow(self, task_id: str, user_prompt: str) -> Path:
        self._run_planning_block(task_id, user_prompt)
        self._run_execution_test_loop(task_id, user_prompt)
        self._run_review_loop(task_id, user_prompt)
        self._run_final_judgement(task_id, user_prompt)
        return self._run_delivery(task_id, user_prompt)

    def _run_bugfix_flow(self, task_id: str, user_prompt: str) -> Path:
        for round_id in range(self.config["limits"]["max_test_fix_rounds"]):
            self.run_role_phase("executor", FIXING, round_id, required_outputs_for("executor", FIXING), user_prompt)
            self._run_patch_merge(task_id, round_id, user_prompt)
            self.run_role_phase("tester", TESTING, round_id, required_outputs_for("tester", TESTING), user_prompt)
            test_decision = self._run_judge_phase(task_id, TEST_JUDGEMENT, round_id, user_prompt)
            if self.judge.is_test_pass(test_decision):
                break
        else:
            raise TaskFailedError("Bugfix testing did not pass within max_test_fix_rounds")
        self._run_final_judgement(task_id, user_prompt)
        return self._run_delivery(task_id, user_prompt)

    def _run_feature_change_flow(self, task_id: str, user_prompt: str) -> Path:
        self._run_planning_block(task_id, user_prompt)
        self._run_execution_test_loop(task_id, user_prompt)
        self._run_review_loop(task_id, user_prompt)
        self._run_final_judgement(task_id, user_prompt)
        return self._run_delivery(task_id, user_prompt)

    def _run_misc_flow(self, task_id: str, user_prompt: str) -> Path:
        self.run_role_phase(
            "executor",
            MISC_RESPONSE,
            0,
            required_outputs_for("executor", MISC_RESPONSE),
            user_prompt,
            agent_count_override=1,
        )
        artifacts = self.repository.list_artifacts(task_id, "response.md")
        if not artifacts:
            raise TaskFailedError("Misc workflow executor did not produce response.md")
        return Path(artifacts[-1]["path"])

    def _run_planning_block(self, task_id: str, user_prompt: str) -> None:
        planner_count = int(self.config["roles"]["planner"]["count"])
        loop_count = self._planning_peer_review_loop_count()
        effective_loop_count = loop_count if planner_count > 1 else 1
        for approval_round in range(int(self.config["limits"]["max_planning_rounds"])):
            final_round_id = approval_round * effective_loop_count
            for loop_round in range(effective_loop_count):
                round_id = (approval_round * effective_loop_count) + loop_round
                final_round_id = round_id
                phase = PLANNING_DRAFT if round_id == 0 else PLANNING_REVISION
                self.run_role_phase("planner", phase, round_id, required_outputs_for("planner", phase), user_prompt)
                if planner_count <= 1:
                    break
                peer_results = self.run_role_phase(
                    "planner",
                    PLANNING_PEER_REVIEW,
                    round_id,
                    required_outputs_for("planner", PLANNING_PEER_REVIEW),
                    user_prompt,
                )
                if self._peer_reviews_satisfied(peer_results):
                    break
            self.run_role_phase(
                "reviewer",
                PLAN_REVIEW,
                final_round_id,
                required_outputs_for("reviewer", PLAN_REVIEW),
                user_prompt,
                agent_count_override=1,
            )
            plan_decision = self._run_judge_phase(task_id, PLAN_JUDGEMENT, final_round_id, user_prompt)
            if self.judge.is_plan_approved(plan_decision):
                return
        raise TaskFailedError("Planning was not approved after peer-review loops")

    def _planning_peer_review_loop_count(self) -> int:
        configured = self.config.get("limits", {}).get(
            "planning_peer_review_loops",
            self.config.get("limits", {}).get("max_planning_rounds", 3),
        )
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            return 3

    def _peer_reviews_satisfied(self, results: list[AgentRunResult]) -> bool:
        if not results:
            return False
        saw_status = False
        for result in results:
            for artifact in result.artifacts:
                if artifact.artifact_type != "peer_review.md" or not artifact.path.exists():
                    continue
                text = artifact.path.read_text(encoding="utf-8", errors="replace").lower()
                if "status: changes_requested" in text:
                    return False
                if "status: satisfied" in text:
                    saw_status = True
        return saw_status

    def _run_execution_test_loop(self, task_id: str, user_prompt: str) -> None:
        self.run_role_phase("executor", EXECUTION, 0, required_outputs_for("executor", EXECUTION), user_prompt)
        self._run_patch_merge(task_id, 0, user_prompt)
        for round_id in range(self.config["limits"]["max_test_fix_rounds"]):
            self.run_role_phase("tester", TESTING, round_id, required_outputs_for("tester", TESTING), user_prompt)
            test_decision = self._run_judge_phase(task_id, TEST_JUDGEMENT, round_id, user_prompt)
            if self.judge.is_test_pass(test_decision):
                break
            # Use next round_id for fixing effort to signal progression in dashboard
            next_round = round_id + 1
            self.run_role_phase("executor", FIXING, next_round, required_outputs_for("executor", FIXING), user_prompt)
            self._run_patch_merge(task_id, next_round, user_prompt)
        else:
            raise TaskFailedError("Testing did not pass within max_test_fix_rounds")

    def _run_review_loop(self, task_id: str, user_prompt: str) -> None:
        for round_id in range(self.config["limits"]["max_review_rounds"]):
            self.run_role_phase("reviewer", REVIEWING, round_id, required_outputs_for("reviewer", REVIEWING), user_prompt)
            review_decision = self._run_judge_phase(task_id, REVIEW_JUDGEMENT, round_id, user_prompt)
            if self.judge.is_review_approved(review_decision):
                break
            
            # Use next round_id for fixing effort to signal progression in dashboard
            next_review_round = round_id + 1
            self.run_role_phase("executor", REVIEW_FIXING, next_review_round, required_outputs_for("executor", REVIEW_FIXING), user_prompt)
            self._run_patch_merge(task_id, next_review_round, user_prompt)
            self._run_regression_test_fix_loop(task_id, user_prompt, next_review_round)
        else:
            raise TaskFailedError("Review was not approved within max_review_rounds")

    def _run_regression_test_fix_loop(self, task_id: str, user_prompt: str, review_round_id: int) -> None:
        max_rounds = int(self.config["limits"]["max_test_fix_rounds"])
        for test_round_id in range(max_rounds):
            phase_round_id = (review_round_id * max_rounds) + test_round_id
            self.run_role_phase(
                "tester",
                REGRESSION_TESTING,
                phase_round_id,
                required_outputs_for("tester", REGRESSION_TESTING),
                user_prompt,
            )
            test_decision = self._run_judge_phase(task_id, TEST_JUDGEMENT, phase_round_id, user_prompt)
            if self.judge.is_test_pass(test_decision):
                return
            
            # Use next phase_round_id for fixing effort
            next_phase_round = phase_round_id + 1
            self.run_role_phase(
                "executor",
                REVIEW_FIXING,
                next_phase_round,
                required_outputs_for("executor", REVIEW_FIXING),
                user_prompt,
            )
            self._run_patch_merge(task_id, next_phase_round, user_prompt)
        raise TaskFailedError("Regression testing did not pass within max_test_fix_rounds")

    def _run_final_judgement(self, task_id: str, user_prompt: str) -> None:
        final_decision = self._run_judge_phase(task_id, FINAL_JUDGEMENT, 0, user_prompt)
        if self.config["policy"].get("require_judge_final_approval", True) and not self.judge.is_final_approved(final_decision):
            raise TaskFailedError("Final judge approval was not granted")

    def _run_delivery(self, task_id: str, user_prompt: str) -> Path:
        self.run_role_phase("communicator", DELIVERY, 0, required_outputs_for("communicator", DELIVERY), user_prompt)
        final_path = self.communicator.latest_final_delivery(task_id)
        if not final_path:
            raise TaskFailedError("Communicator did not produce final_delivery.md")
        return self._publish_delivery(task_id, final_path)

    def run_role_phase(
        self,
        role: str,
        phase: str,
        round_id: int,
        required_outputs: list[str],
        user_prompt: str | None = None,
        agent_count_override: int | None = None,
    ) -> list[AgentRunResult]:
        task_id = self._single_active_task_id(user_prompt)
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        user_prompt = user_prompt if user_prompt is not None else task["user_prompt"]
        agent_count = agent_count_override if agent_count_override is not None else int(self.config["roles"][role]["count"])

        # Check for already completed/recoverable phase to support checkpoint resume.
        # Older Harness versions could mark a phase FAILED after all concurrent
        # agents had completed. If all expected agent runs completed, recover the
        # phase instead of repeating expensive agent work.
        existing_phases = self.repository.list_phases(task_id)
        checkpoint_candidates = [
            p
            for p in existing_phases
            if p["phase_type"] == phase
            and p["role"] == role
            and p["round_id"] == round_id
            and p["status"] in {"COMPLETED", "FAILED"}
        ]
        checkpoint_phase = None
        checkpoint_results: list[AgentRunResult] = []
        for candidate in reversed(checkpoint_candidates):
            candidate_results = self._recover_phase_results(task_id, candidate["phase_id"])
            recoverable = (
                len({result.agent_id for result in candidate_results}) >= agent_count
                and self._recovered_results_have_required_outputs(candidate_results, required_outputs)
            )
            if recoverable:
                checkpoint_phase = candidate
                checkpoint_results = candidate_results
                break
        if checkpoint_phase:
            phase_id = checkpoint_phase["phase_id"]
            if checkpoint_phase["status"] == "FAILED":
                self.repository.update_phase_status(phase_id, "COMPLETED")
                self.logger.info(
                    "Recovered failed phase %s %s round=%s from completed agent runs",
                    role,
                    phase,
                    round_id,
                )
            self.logger.info("Resuming task %s: Skipping completed phase %s %s round=%s", task_id, role, phase, round_id)
            self._emit(
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
            self._emit(
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

        self.repository.update_task(task_id, status=phase, current_phase=phase, current_role=role)
        phase_id = self.repository.create_phase(task_id, phase, role, round_id)
        timeout_seconds = int(self.config["timeouts"][role])
        backend = self._backend_for(role)
        adapter = self._adapter_for_backend(backend)
        agent_ids = [f"{role}-{index + 1}" for index in range(agent_count)]
        self.logger.info("Running %s phase %s with %s agent(s)", role, phase, agent_count)
        phase_started_at = time.monotonic()
        self._emit(
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
            if self.config["policy"].get("same_role_can_run_concurrently", True) and agent_count > 1:
                results = self._run_agents_concurrently(
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
                    self._run_agent_with_retry(
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
            self.repository.update_phase_status(phase_id, "COMPLETED")
            elapsed_seconds = round(time.monotonic() - phase_started_at, 3)
            self._emit(
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
            self.repository.update_phase_status(phase_id, "FAILED")
            elapsed_seconds = round(time.monotonic() - phase_started_at, 3)
            self._emit(
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

    def _recover_phase_results(self, task_id: str, phase_id: str) -> list[AgentRunResult]:
        runs = self.repository.list_agent_runs(task_id)
        phase_runs = [run for run in runs if run["phase_id"] == phase_id and run["status"] == "COMPLETED"]
        artifacts = self.repository.list_artifacts(task_id)
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

    def _recovered_results_have_required_outputs(self, results: list[AgentRunResult], required_outputs: list[str]) -> bool:
        for result in results:
            artifacts_by_type = {artifact.artifact_type: artifact.path for artifact in result.artifacts}
            for output_name in required_outputs:
                path = artifacts_by_type.get(output_name)
                if not path or not path.exists() or not path.is_file() or path.stat().st_size == 0:
                    return False
            delivery_path = artifacts_by_type.get("delivery.md")
            if delivery_path and self.validator.parse_delivery_status(delivery_path) != "success":
                return False
        return bool(results)

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
        max_retry = int(self.config["limits"]["max_agent_retry"])
        per_attempt_grace_seconds = 10
        phase_timeout_seconds = None
        if timeout_seconds > 0:
            phase_timeout_seconds = (max_retry + 1) * (timeout_seconds + per_attempt_grace_seconds)
        cancel_event = threading.Event()
        executor = ThreadPoolExecutor(max_workers=len(agent_ids))
        futures = {
            executor.submit(
                self._run_agent_with_retry,
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
        cancel_event: threading.Event | None = None,
    ) -> AgentRunResult:
        max_retry = int(self.config["limits"]["max_agent_retry"])
        last_result: AgentRunResult | None = None
        last_error_message: str | None = None
        for attempt in range(max_retry + 1):
            if cancel_event and cancel_event.is_set():
                raise TaskFailedError(f"Agent {agent_id} cancelled because the phase timed out")
            attempt_started_at = time.monotonic()
            run_id = self.repository.create_agent_run(task_id, phase_id, role, agent_id, attempt)
            workspace = self.workspace_manager.create_workspace(
                task_id,
                phase_id,
                role,
                agent_id,
                round_id,
                attempt,
                source_repo=self._source_repo_for_workspace(),
            )
            input_artifacts = self._stage_input_artifacts(task_id, workspace.input_dir, role, phase, exclude_phase_id=phase_id)
            context = AgentRunContext(
                task_id=task_id,
                phase_id=phase_id,
                phase=phase,
                role=role,
                agent_id=agent_id,
                round_id=round_id,
                user_prompt=user_prompt,
                role_instruction=ROLE_INSTRUCTIONS.get(role, ""),
                workspace_dir=workspace.workspace_dir,
                repo_dir=workspace.repo_dir,
                input_dir=workspace.input_dir,
                output_dir=workspace.output_dir,
                log_dir=workspace.log_dir,
                input_artifacts=input_artifacts,
                required_outputs=required_outputs,
                timeout_seconds=timeout_seconds,
                config=self.config,
            )
            context.log_dir.mkdir(parents=True, exist_ok=True)
            (context.log_dir / "prompt.md").write_text(self.prompt_builder.build(context), encoding="utf-8")
            self._emit(
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
                        "backend": self._backend_for(role),
                        "workspace": str(context.workspace_dir),
                        "output": str(context.output_dir),
                        "logs": str(context.log_dir),
                    },
                )
            )
            try:
                result = self._run_adapter_with_heartbeat(adapter, context, attempt)
                if cancel_event and cancel_event.is_set():
                    message = "Phase timed out before this agent result was accepted; ignoring late result"
                    self.repository.update_agent_run_status(run_id, "TIMEOUT", message)
                    raise TaskFailedError(message)
                ok, errors = self.validator.validate_required_outputs(workspace.output_dir, required_outputs)
                delivery_status = self.validator.parse_delivery_status(workspace.output_dir / "delivery.md")
                result.validation_ok = ok
                result.validation_errors = errors
                if result.status == "COMPLETED" and result.exit_code == 0 and ok:
                    result.artifacts = self.artifact_manager.collect_output_dir(
                        task_id, phase_id, role, agent_id, workspace.output_dir
                    )
                    self.repository.update_agent_run_status(run_id, "COMPLETED")
                    elapsed_seconds = round(time.monotonic() - attempt_started_at, 3)
                    self._emit(
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
                last_error_message = message
                self.repository.update_agent_run_status(run_id, status, message)
                elapsed_seconds = round(time.monotonic() - attempt_started_at, 3)
                diagnostics_path = context.log_dir / "request_diagnostics.md"
                event_data = {
                    "logs": str(context.log_dir),
                    "delivery_status": delivery_status or "-",
                    "elapsed_seconds": elapsed_seconds,
                }
                if diagnostics_path.exists():
                    event_data["diagnostics"] = str(diagnostics_path)
                self._emit(
                    ProgressEvent(
                        "agent_retryable_failure",
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
                last_result = result
            except Exception as exc:
                last_error_message = str(exc)
                failure_status = "TIMEOUT" if cancel_event and cancel_event.is_set() else "FAILED"
                self.repository.update_agent_run_status(run_id, failure_status, str(exc))
                elapsed_seconds = round(time.monotonic() - attempt_started_at, 3)
                diagnostics_path = context.log_dir / "request_diagnostics.md"
                event_data = {"logs": str(context.log_dir), "elapsed_seconds": elapsed_seconds}
                if diagnostics_path.exists():
                    event_data["diagnostics"] = str(diagnostics_path)
                self._emit(
                    ProgressEvent(
                        "agent_retryable_failure",
                        task_id=task_id,
                        phase=phase,
                        role=role,
                        agent_id=agent_id,
                        round_id=round_id,
                        attempt=attempt,
                        status=failure_status,
                        message=str(exc),
                        data=event_data,
                    )
                )
                last_result = AgentRunResult(task_id, phase_id, role, agent_id, "FAILED", exit_code=1)
            if attempt >= max_retry:
                break
        if last_result:
            details = last_result.validation_errors or ([last_error_message] if last_error_message else [])
            raise TaskFailedError(
                f"Agent {agent_id} failed after {max_retry + 1} attempt(s): {details}"
            )
        raise TaskFailedError(f"Agent {agent_id} failed before producing a result")

    def _run_adapter_with_heartbeat(self, adapter: AgentAdapter, context: AgentRunContext, attempt: int) -> AgentRunResult:
        interval = float(self.config.get("heartbeat", {}).get("interval_seconds", 60))
        if interval <= 0:
            return adapter.run(context)
        stop_event = threading.Event()
        started_at = time.monotonic()

        def beat() -> None:
            while not stop_event.wait(interval):
                elapsed_seconds = int(time.monotonic() - started_at)
                self._emit(
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
                            "backend": self._backend_for(context.role),
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
        self.repository.create_judge_decision(task_id, phase_id, phase, normalized)
        return normalized

    def _run_patch_merge(self, task_id: str, round_id: int, user_prompt: str) -> None:
        self.run_role_phase(
            "executor",
            PATCH_MERGE,
            round_id,
            required_outputs_for("executor", PATCH_MERGE),
            user_prompt,
            agent_count_override=1,
        )

    def _backend_for(self, role: str) -> str:
        return self.config["agent_backend"].get(role) or self.config["agent_backend"].get("default", "mock")

    def _adapter_for_backend(self, backend: str) -> AgentAdapter:
        if backend == "mock":
            return MockAgentAdapter()
        if backend == "codex":
            return CodexCLIAdapter()
        if backend == "claude":
            return ClaudeCodeAdapter()
        raise ValueError(f"Unsupported agent backend: {backend}")

    def _source_repo_for_workspace(self) -> Path | None:
        source_repo = self.config.get("system", {}).get("source_repo")
        if not source_repo:
            return None
        if self._active_workflow_type not in {BUGFIX, FEATURE_CHANGE}:
            return None
        path = Path(str(source_repo)).expanduser().resolve()
        return path if path.exists() and path.is_dir() else None

    def _single_active_task_id(self, user_prompt: str | None) -> str:
        if self._active_task_id:
            return self._active_task_id
        if user_prompt is not None:
            with self.repository.db.connect() as conn:
                row = conn.execute(
                    "SELECT task_id FROM tasks WHERE user_prompt = ? ORDER BY created_at DESC LIMIT 1",
                    (user_prompt,),
                ).fetchone()
            if row:
                return row["task_id"]
        with self.repository.db.connect() as conn:
            row = conn.execute("SELECT task_id FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            raise TaskFailedError("No task exists")
        return row["task_id"]

    def _emit(self, event: ProgressEvent) -> None:
        if self.progress_callback:
            self.progress_callback(event)

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
    ) -> list[Path]:
        artifacts = self.repository.list_artifacts(task_id)
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
            and self._artifact_visible_to(target_role, phase, artifact)
        ]
        for index, artifact in enumerate(reversed(visible_artifacts), start=1):
            source = Path(artifact["path"])
            if not source.exists():
                continue
            source_size = source.stat().st_size
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
                max_file_bytes=limits["max_file_bytes"],
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

    def _artifact_visible_to(self, role: str, phase: str, artifact: dict[str, Any]) -> bool:
        artifact_type = artifact["artifact_type"]
        artifact_role = artifact["role"] or ""
        if artifact_type == "project_context.md":
            return True
        if role == "planner":
            return artifact_role in {"planner", "judge"} and artifact_type in {
                "plan.md",
                "assumptions.md",
                "risk.md",
                "todo_breakdown.md",
                "peer_review.md",
                "decision.json",
                "decision_summary.md",
            }
        if role == "executor":
            if phase == EXECUTION:
                return artifact_role in {"planner", "reviewer", "judge"} and artifact_type in {
                    "plan.md",
                    "assumptions.md",
                    "risk.md",
                    "todo_breakdown.md",
                    "peer_review.md",
                    "review_report.md",
                    "decision.json",
                    "decision_summary.md",
                }
            if phase == PATCH_MERGE:
                return artifact_role in {"planner", "executor", "tester", "reviewer", "judge", "orchestrator"} and artifact_type in {
                    "plan.md",
                    "assumptions.md",
                    "risk.md",
                    "todo_breakdown.md",
                    "implementation_plan.md",
                    "changed_files.md",
                    "patch.diff",
                    "fix_schedule.md",
                    "fix_patch.diff",
                    "fix_notes.md",
                    "self_check.md",
                    "build_report.md",
                    "test_report.md",
                    "bug_report.md",
                    "review_report.md",
                    "decision.json",
                    "decision_summary.md",
                    "merged_patch.diff",
                    "merge_report.md",
                }
            if phase == FIXING:
                return artifact_role in {"executor", "tester", "judge", "orchestrator"} and artifact_type in {
                    "implementation_plan.md",
                    "changed_files.md",
                    "merged_patch.diff",
                    "self_check.md",
                    "merge_report.md",
                    "build_report.md",
                    "test_report.md",
                    "bug_report.md",
                    "decision.json",
                    "decision_summary.md",
                }
            if phase == REVIEW_FIXING:
                return artifact_role in {"executor", "tester", "reviewer", "judge", "orchestrator"} and artifact_type in {
                    "implementation_plan.md",
                    "changed_files.md",
                    "merged_patch.diff",
                    "self_check.md",
                    "merge_report.md",
                    "build_report.md",
                    "test_report.md",
                    "bug_report.md",
                    "review_report.md",
                    "decision.json",
                    "decision_summary.md",
                }
            return False
        if role == "tester":
            return artifact_role in {"executor", "orchestrator"} and artifact_type in {
                "implementation_plan.md",
                "changed_files.md",
                "merged_patch.diff",
                "fix_schedule.md",
                "fix_notes.md",
                "self_check.md",
                "merge_report.md",
            }
        if role == "reviewer":
            if phase == PLAN_REVIEW:
                return artifact_role in {"planner", "judge"} and artifact_type in {
                    "plan.md",
                    "assumptions.md",
                    "risk.md",
                    "todo_breakdown.md",
                    "peer_review.md",
                    "decision.json",
                    "decision_summary.md",
                }
            return artifact_role in {"executor", "tester", "judge", "orchestrator"} and artifact_type in {
                "implementation_plan.md",
                "changed_files.md",
                "merged_patch.diff",
                "fix_schedule.md",
                "fix_notes.md",
                "self_check.md",
                "merge_report.md",
                "build_report.md",
                "test_report.md",
                "bug_report.md",
                "decision.json",
                "decision_summary.md",
            }
        if role == "judge":
            if phase == PLAN_JUDGEMENT:
                return artifact_role in {"planner", "reviewer"} and artifact_type in {
                    "plan.md",
                    "assumptions.md",
                    "risk.md",
                    "todo_breakdown.md",
                    "peer_review.md",
                    "review_report.md",
                }
            if phase == TEST_JUDGEMENT:
                return (
                    artifact_role in {"executor", "tester", "orchestrator"}
                    and artifact_type not in {"patch.diff", "fix_patch.diff"}
                )
            if phase == REVIEW_JUDGEMENT:
                return (
                    artifact_role in {"executor", "tester", "reviewer", "orchestrator"}
                    and artifact_type not in {"patch.diff", "fix_patch.diff"}
                )
            if phase == FINAL_JUDGEMENT:
                return (
                    artifact_role in {"planner", "executor", "tester", "reviewer", "judge", "orchestrator"}
                    and artifact_type not in {"patch.diff", "fix_patch.diff"}
                )
            return False
        if role == "communicator":
            return (
                artifact_role in {"planner", "executor", "tester", "reviewer", "judge", "orchestrator"}
                and artifact_type not in {"patch.diff", "fix_patch.diff"}
            )
        return True

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
        source_files = self._publish_materialized_source(project_dir)
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
            "merge_report.md",
            "patch.diff",
            "fix_patch.diff",
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

    def _publish_materialized_source(self, project_dir: Path) -> list[Path]:
        patch_path = project_dir / "patches" / "final.patch"
        source_dir = project_dir / "source"
        if source_dir.exists():
            shutil.rmtree(source_dir)
        if not patch_path.exists():
            return []
        files = self._materialized_files_from_unified_diff(
            patch_path.read_text(encoding="utf-8", errors="replace"),
            self._configured_source_repo(),
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

    def _slugify_project_name(self, prompt: str) -> str:
        ascii_prompt = prompt.encode("ascii", "ignore").decode("ascii").lower()
        compact = re.sub(r"[^a-z0-9]+", "-", ascii_prompt).strip("-")
        compact = re.sub(r"-+", "-", compact)[:32].strip("-")
        return compact or "project"
