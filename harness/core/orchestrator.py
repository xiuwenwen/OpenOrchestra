from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from harness.adapters.base import AgentAdapter
from harness.adapters.claude_code_adapter import ClaudeCodeAdapter, REQUEST_SIZE_ERROR_PATTERNS
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


class NonRetryableAgentError(TaskFailedError):
    """Agent failure that cannot be fixed by rerunning the same prompt."""


MATERIALIZED_SUCCESS_MARKER = ".harness_materialized_success.json"
SINGLE_EXECUTOR_FIX_PHASES = {FIXING, REVIEW_FIXING}
FORBIDDEN_PATCH_TOP_LEVEL_NAMES = {"artifacts", "deliver", "deliveries", "workspaces"}
FORBIDDEN_PATCH_PATH_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
FORBIDDEN_PATCH_FILE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
FORBIDDEN_PATCH_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


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
        self._active_task_resume_status: str | None = None
        self.progress_callback = progress_callback

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
        
        # Persist workflow_type if not already set
        if not task.get("workflow_type"):
            with self.repository._lock, self.repository.db.connect() as conn:
                conn.execute("UPDATE tasks SET workflow_type = ? WHERE task_id = ?", (workflow_type, task_id))
        
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
            self._active_task_resume_status = None

    def _run_new_project_flow(self, task_id: str, user_prompt: str) -> Path:
        self._run_planning_block(task_id, user_prompt)
        self._run_execution_test_loop(task_id, user_prompt)
        self._run_review_loop(task_id, user_prompt)
        self._run_final_judgement(task_id, user_prompt)
        return self._run_delivery(task_id, user_prompt)

    def _run_bugfix_flow(self, task_id: str, user_prompt: str) -> Path:
        max_rounds = int(self.config["limits"]["max_test_fix_rounds"])
        start_round = self._bugfix_resume_start_round(task_id)
        for round_id in range(start_round, start_round + max_rounds):
            self.run_role_phase("executor", FIXING, round_id, required_outputs_for("executor", FIXING), user_prompt)
            merge_ok = self._run_patch_merge(task_id, round_id, user_prompt)
            if not merge_ok:
                continue
            self.run_role_phase("tester", TESTING, round_id, required_outputs_for("tester", TESTING), user_prompt)
            self._run_harness_test_gate(task_id, round_id)
            test_decision = self._run_judge_phase(task_id, TEST_JUDGEMENT, round_id, user_prompt)
            if self.judge.is_test_pass(test_decision):
                break
        else:
            raise TaskFailedError("Bugfix testing did not pass within max_test_fix_rounds")
        self._run_review_loop(task_id, user_prompt)
        self._run_final_judgement(task_id, user_prompt)
        return self._run_delivery(task_id, user_prompt)

    def _bugfix_resume_start_round(self, task_id: str) -> int:
        if self._active_task_id != task_id or self._active_task_resume_status != FAILED:
            return 0
        highest_round = self._highest_bugfix_round_id(task_id)
        if highest_round is None:
            return 0
        max_rounds = int(self.config["limits"]["max_test_fix_rounds"])
        if highest_round + 1 < max_rounds:
            return 0
        return highest_round + 1

    def _highest_bugfix_round_id(self, task_id: str) -> int | None:
        rounds = [
            int(phase["round_id"])
            for phase in self.repository.list_phases(task_id)
            if phase["phase_type"] in {FIXING, PATCH_MERGE, TESTING, TEST_JUDGEMENT}
            and phase["round_id"] is not None
        ]
        return max(rounds) if rounds else None

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
        merge_ok = self._run_patch_merge(task_id, 0, user_prompt)
        max_rounds = int(self.config["limits"]["max_test_fix_rounds"])
        for round_id in range(max_rounds):
            if merge_ok:
                self.run_role_phase("tester", TESTING, round_id, required_outputs_for("tester", TESTING), user_prompt)
                self._run_harness_test_gate(task_id, round_id)
                test_decision = self._run_judge_phase(task_id, TEST_JUDGEMENT, round_id, user_prompt)
                if self.judge.is_test_pass(test_decision):
                    break
            # Use next round_id for fixing effort to signal progression in dashboard
            next_round = round_id + 1
            self.run_role_phase("executor", FIXING, next_round, required_outputs_for("executor", FIXING), user_prompt)
            merge_ok = self._run_patch_merge(task_id, next_round, user_prompt)
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
            merge_ok = self._run_patch_merge(task_id, next_review_round, user_prompt)
            self._run_regression_test_fix_loop(task_id, user_prompt, next_review_round, merge_ok)
        else:
            raise TaskFailedError("Review was not approved within max_review_rounds")

    def _run_regression_test_fix_loop(self, task_id: str, user_prompt: str, review_round_id: int, merge_ok: bool) -> None:
        max_rounds = int(self.config["limits"]["max_test_fix_rounds"])
        for test_round_id in range(max_rounds):
            phase_round_id = (review_round_id * max_rounds) + test_round_id
            if merge_ok:
                self.run_role_phase(
                    "tester",
                    REGRESSION_TESTING,
                    phase_round_id,
                    required_outputs_for("tester", REGRESSION_TESTING),
                    user_prompt,
                )
                self._run_harness_test_gate(task_id, phase_round_id)
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
            merge_ok = self._run_patch_merge(task_id, next_phase_round, user_prompt)
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
        agent_count = self._effective_agent_count(role, phase, agent_count_override)

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
            self._prepare_materialized_workspace_repo(task_id, role, phase, workspace.repo_dir)
            input_artifacts = self._stage_input_artifacts(
                task_id,
                workspace.input_dir,
                role,
                phase,
                exclude_phase_id=phase_id,
                round_id=round_id,
            )
            task_for_metadata = self.repository.get_task(task_id) or {"task_id": task_id, "user_prompt": user_prompt}
            metadata = self._context_metadata(task_for_metadata, role, phase)
            metadata.update(self._repo_context_metadata(task_id, role, phase))
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
                metadata=metadata,
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
                terminal_failure = self._is_request_size_failure(result, context, message)
                if terminal_failure:
                    status = "FAILED"
                    message = self._request_size_failure_message(context)
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
                terminal_failure = self._text_contains_request_size_error(str(exc)) or self._logs_contain_request_size_error(
                    context.log_dir
                )
                status_message = str(exc)
                if terminal_failure:
                    last_error_message = self._request_size_failure_message(context)
                    status_message = last_error_message
                self.repository.update_agent_run_status(run_id, failure_status, status_message)
                elapsed_seconds = round(time.monotonic() - attempt_started_at, 3)
                diagnostics_path = context.log_dir / "request_diagnostics.md"
                event_data = {"logs": str(context.log_dir), "elapsed_seconds": elapsed_seconds}
                if diagnostics_path.exists():
                    event_data["diagnostics"] = str(diagnostics_path)
                self._emit(
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
            raise TaskFailedError(
                f"Agent {agent_id} failed after {max_retry + 1} attempt(s): {details}"
            )
        raise TaskFailedError(f"Agent {agent_id} failed before producing a result")

    def _is_request_size_failure(self, result: AgentRunResult, context: AgentRunContext, message: str) -> bool:
        if self._text_contains_request_size_error(message):
            return True
        texts = []
        for path in (result.stdout_path, result.stderr_path, context.log_dir / "request_diagnostics.md"):
            if path and path.exists():
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        return self._text_contains_request_size_error("\n".join(texts))

    def _logs_contain_request_size_error(self, log_dir: Path) -> bool:
        texts = []
        for name in ("stdout.log", "stderr.log", "request_diagnostics.md"):
            path = log_dir / name
            if path.exists():
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        return self._text_contains_request_size_error("\n".join(texts))

    def _text_contains_request_size_error(self, text: str) -> bool:
        return any(pattern in text for pattern in REQUEST_SIZE_ERROR_PATTERNS)

    def _request_size_failure_message(self, context: AgentRunContext) -> str:
        diagnostics_path = context.log_dir / "request_diagnostics.md"
        return (
            "Agent request exceeded the model context/request-size budget; not retrying the same prompt. "
            f"Lower claude.max_output_tokens for role={context.role}, reduce staged artifact input, or use a larger model window. "
            f"Diagnostics: {diagnostics_path}"
        )

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
        gate_status = self._objective_gate_status(task_id, round_id)
        if gate_status == "pass":
            test_gate_status = self._test_gate_status(task_id, round_id)
            if test_gate_status == "pass" or (test_gate_status == "skipped" and not self._require_harness_test_commands()):
                return payload
            reason = (
                "Harness test gate is missing."
                if test_gate_status is None
                else f"Harness test gate status is {test_gate_status}."
            )
            return {
                **payload,
                "decision": "fail",
                "tests_passed": False,
                "test_gate_status": test_gate_status or "missing",
                "reason": f"{reason} LLM judge cannot override missing or failed Harness-run test evidence.",
            }
        reason = "Objective gate is missing." if gate_status is None else f"Objective gate status is {gate_status}."
        return {
            **payload,
            "decision": "fail",
            "tests_passed": False,
            "objective_gate_status": gate_status or "missing",
            "reason": f"{reason} LLM judge cannot override objective gate failure.",
        }

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
                    completed = subprocess.run(
                        command,
                        cwd=repo_dir,
                        text=True,
                        shell=True,
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
        scope_ok, scope_errors, changed_files = self._validate_patch_scope(patch_path)
        report = self._validate_patch_apply_check(patch_path, source_repo)
        status = self._patch_validation_status(report)
        materialize_status = "skipped"
        diff_check_status = "skipped"
        materialized_repo: Path | None = None
        materialize_report = self._materialized_repo_report(
            task_id=task_id,
            round_id=round_id,
            patch_path=patch_path,
            source_repo=source_repo,
            status="skipped",
            diff_check_status=diff_check_status,
            repo_path=None,
            stdout="",
            stderr=self._materialization_skip_reason(status, scope_ok, scope_errors),
            exit_code=None,
            diff_check_stdout="",
            diff_check_stderr="",
            diff_check_exit_code=None,
        )
        if status == "pass" and scope_ok:
            materialized_repo, materialize_report = self._materialize_merged_patch_repo(
                task_id,
                round_id,
                patch_path,
                source_repo,
                changed_files,
            )
            materialize_status = self._materialized_repo_status(materialize_report)
            diff_check_status = self._materialized_repo_field(materialize_report, "diff_check_status") or "unknown"
            if materialized_repo is None:
                if materialize_status in {"failed", "skipped"}:
                    status = "fail"
        objective_status = "pass" if scope_ok and status == "pass" and materialize_status == "success" and diff_check_status == "pass" else "fail"
        objective_report = self._objective_gate_report(
            task_id=task_id,
            round_id=round_id,
            patch_path=patch_path,
            source_repo=source_repo,
            status=objective_status,
            scope_ok=scope_ok,
            scope_errors=scope_errors,
            changed_files=changed_files,
            patch_apply_status=status,
            materialize_status=materialize_status,
            diff_check_status=diff_check_status,
        )
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
                status=objective_status.upper(),
                message=f"Objective patch gate {objective_status}",
                data={
                    "artifacts": 3,
                    "patch_validation": str(ref.path),
                    "materialized_repo_report": str(materialized_ref.path),
                    "objective_gate": str(objective_ref.path),
                    "materialized_repo": str(materialized_repo) if materialized_repo else "-",
                },
            )
        )
        return objective_status == "pass"

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

    def _validate_patch_scope(self, patch_path: Path) -> tuple[bool, list[str], list[Path]]:
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        changed_files = self._changed_files_from_unified_diff(patch_text)
        errors: list[str] = []
        if not changed_files:
            errors.append("merged_patch.diff does not contain any changed files")
        for relative_path in changed_files:
            errors.extend(self._patch_path_scope_errors(relative_path))
        return not errors, errors, changed_files

    def _changed_files_from_unified_diff(self, patch_text: str) -> list[Path]:
        changed: list[Path] = []
        for line in patch_text.splitlines():
            if not line.startswith("diff --git "):
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            for raw_path in (parts[2], parts[3]):
                path = self._strip_diff_path(raw_path)
                if path != Path("/dev/null") and path not in changed:
                    changed.append(path)
        return changed

    def _patch_path_scope_errors(self, relative_path: Path) -> list[str]:
        errors: list[str] = []
        if not self._is_safe_relative_path(relative_path):
            return [f"unsafe path: {relative_path}"]
        parts = set(relative_path.parts)
        if relative_path.parts and relative_path.parts[0] in FORBIDDEN_PATCH_TOP_LEVEL_NAMES:
            errors.append(f"forbidden generated top-level path: {relative_path}")
        forbidden_parts = sorted(parts & FORBIDDEN_PATCH_PATH_PARTS)
        if forbidden_parts:
            errors.append(f"forbidden path component(s) {', '.join(forbidden_parts)} in {relative_path}")
        name = relative_path.name
        if name in FORBIDDEN_PATCH_FILE_NAMES or name.startswith(".env."):
            errors.append(f"forbidden sensitive file path: {relative_path}")
        if relative_path.suffix in FORBIDDEN_PATCH_SUFFIXES:
            errors.append(f"forbidden sensitive file suffix: {relative_path}")
        return errors

    def _materialization_skip_reason(self, patch_status: str, scope_ok: bool, scope_errors: list[str]) -> str:
        if not scope_ok:
            return "Patch scope gate failed; materialization was not run.\n" + "\n".join(scope_errors)
        return f"Patch validation status was {patch_status}; materialization only runs when status is pass."

    def _objective_gate_report(
        self,
        *,
        task_id: str,
        round_id: int,
        patch_path: Path,
        source_repo: Path | None,
        status: str,
        scope_ok: bool,
        scope_errors: list[str],
        changed_files: list[Path],
        patch_apply_status: str,
        materialize_status: str,
        diff_check_status: str,
    ) -> str:
        return "\n".join(
            [
                "# Objective Gate",
                "",
                f"status: {status}",
                f"task_id: {task_id}",
                f"round_id: {round_id}",
                f"patch: {patch_path}",
                f"source_repo: {source_repo or 'none'}",
                f"scope_status: {'pass' if scope_ok else 'fail'}",
                f"patch_apply_status: {patch_apply_status}",
                f"materialize_status: {materialize_status}",
                f"diff_check_status: {diff_check_status}",
                "",
                "## Changed Files",
                "",
                *(f"- {path}" for path in changed_files),
                "",
                "## Scope Errors",
                "",
                *(f"- {error}" for error in scope_errors),
                "",
            ]
        )

    def _validate_patch_apply_check(self, patch_path: Path, source_repo: Path | None) -> str:
        if not shutil.which("git"):
            return self._patch_validation_report(
                patch_path=patch_path,
                source_repo=source_repo,
                status="skipped",
                exit_code=None,
                stdout="",
                stderr="git executable was not found on PATH.",
            )
        with tempfile.TemporaryDirectory(prefix="harness-patch-check-") as tmp:
            check_dir = Path(tmp) / "repo"
            if source_repo:
                self._copy_source_for_patch_validation(source_repo, check_dir)
            else:
                check_dir.mkdir(parents=True, exist_ok=True)
            command = ["git", "apply", "--check", "--whitespace=nowarn", str(patch_path)]
            completed = subprocess.run(
                command,
                cwd=check_dir,
                text=True,
                capture_output=True,
                check=False,
            )
            status = "pass" if completed.returncode == 0 else "fail"
            return self._patch_validation_report(
                patch_path=patch_path,
                source_repo=source_repo,
                status=status,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

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

    def _patch_validation_report(
        self,
        *,
        patch_path: Path,
        source_repo: Path | None,
        status: str,
        exit_code: int | None,
        stdout: str,
        stderr: str,
    ) -> str:
        return "\n".join(
            [
                "# Patch Validation",
                "",
                f"status: {status}",
                f"patch: {patch_path}",
                f"source_repo: {source_repo or 'none'}",
                "command: git apply --check --whitespace=nowarn <merged_patch.diff>",
                f"exit_code: {exit_code if exit_code is not None else 'n/a'}",
                "",
                "## stdout",
                "",
                "```text",
                stdout.strip(),
                "```",
                "",
                "## stderr",
                "",
                "```text",
                stderr.strip(),
                "```",
                "",
            ]
        )

    def _patch_validation_status(self, report: str) -> str:
        for line in report.splitlines():
            if line.startswith("status: "):
                return line.split(":", 1)[1].strip().lower()
        return "unknown"

    def _materialized_repo_status(self, report: str) -> str:
        return self._materialized_repo_field(report, "status") or "unknown"

    def _materialized_repo_field(self, report: str, field_name: str) -> str | None:
        prefix = f"{field_name}: "
        for line in report.splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip().lower()
        return None

    def _materialize_merged_patch_repo(
        self,
        task_id: str,
        round_id: int,
        patch_path: Path,
        source_repo: Path | None,
        changed_files: list[Path] | None = None,
    ) -> tuple[Path | None, str]:
        if not shutil.which("git"):
            return None, self._materialized_repo_report(
                task_id=task_id,
                round_id=round_id,
                patch_path=patch_path,
                source_repo=source_repo,
                status="skipped",
                diff_check_status="skipped",
                repo_path=None,
                stdout="",
                stderr="git executable was not found on PATH.",
                exit_code=None,
                diff_check_stdout="",
                diff_check_stderr="",
                diff_check_exit_code=None,
            )
        repo_dir = self._materialized_repo_dir(task_id, round_id)
        tmp_repo_dir = repo_dir.parent / f".repo_tmp_{uuid.uuid4().hex}"
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        if source_repo:
            self._copy_source_for_patch_validation(source_repo, tmp_repo_dir)
        else:
            tmp_repo_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_diff_check_repo(tmp_repo_dir)
        completed = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_path)],
            cwd=tmp_repo_dir,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            if tmp_repo_dir.exists():
                shutil.rmtree(tmp_repo_dir)
            return None, self._materialized_repo_report(
                task_id=task_id,
                round_id=round_id,
                patch_path=patch_path,
                source_repo=source_repo,
                status="failed",
                diff_check_status="skipped",
                repo_path=None,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.returncode,
                diff_check_stdout="",
                diff_check_stderr="",
                diff_check_exit_code=None,
            )
        self._stage_new_files_for_diff_check(tmp_repo_dir, changed_files or [])
        diff_check = subprocess.run(
            ["git", "diff", "--check"],
            cwd=tmp_repo_dir,
            text=True,
            capture_output=True,
            check=False,
        )
        if diff_check.returncode != 0:
            if tmp_repo_dir.exists():
                shutil.rmtree(tmp_repo_dir)
            return None, self._materialized_repo_report(
                task_id=task_id,
                round_id=round_id,
                patch_path=patch_path,
                source_repo=source_repo,
                status="failed",
                diff_check_status="fail",
                repo_path=None,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.returncode,
                diff_check_stdout=diff_check.stdout,
                diff_check_stderr=diff_check.stderr,
                diff_check_exit_code=diff_check.returncode,
            )
        self._write_materialized_success_marker(tmp_repo_dir, task_id, round_id, patch_path)
        tmp_repo_dir.rename(repo_dir)
        return repo_dir, self._materialized_repo_report(
            task_id=task_id,
            round_id=round_id,
            patch_path=patch_path,
            source_repo=source_repo,
            status="success",
            diff_check_status="pass",
            repo_path=repo_dir,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            diff_check_stdout=diff_check.stdout,
            diff_check_stderr=diff_check.stderr,
            diff_check_exit_code=diff_check.returncode,
        )

    def _initialize_diff_check_repo(self, repo_dir: Path) -> None:
        if not shutil.which("git"):
            return
        subprocess.run(["git", "init", "-q"], cwd=repo_dir, text=True, capture_output=True, check=False)
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, text=True, capture_output=True, check=False)

    def _stage_new_files_for_diff_check(self, repo_dir: Path, changed_files: list[Path]) -> None:
        safe_paths = [str(path) for path in changed_files if self._is_safe_relative_path(path)]
        if not safe_paths:
            return
        subprocess.run(
            ["git", "add", "-N", "--", *safe_paths],
            cwd=repo_dir,
            text=True,
            capture_output=True,
            check=False,
        )

    def _write_materialized_success_marker(self, repo_dir: Path, task_id: str, round_id: int, patch_path: Path) -> None:
        marker = {
            "status": "success",
            "task_id": task_id,
            "round_id": round_id,
            "patch_path": str(patch_path),
            "patch_hash": sha256_file(patch_path) if patch_path.exists() else None,
        }
        (repo_dir / MATERIALIZED_SUCCESS_MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _materialized_repo_report(
        self,
        *,
        task_id: str,
        round_id: int,
        patch_path: Path,
        source_repo: Path | None,
        status: str,
        diff_check_status: str,
        repo_path: Path | None,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        diff_check_stdout: str,
        diff_check_stderr: str,
        diff_check_exit_code: int | None,
    ) -> str:
        return "\n".join(
            [
                "# Materialized Repository",
                "",
                f"status: {status}",
                f"task_id: {task_id}",
                f"round_id: {round_id}",
                f"repo_path: {repo_path or 'none'}",
                f"patch: {patch_path}",
                f"source_repo: {source_repo or 'none'}",
                f"diff_check_status: {diff_check_status}",
                "command: git apply --whitespace=nowarn <merged_patch.diff>",
                f"exit_code: {exit_code if exit_code is not None else 'n/a'}",
                "diff_check_command: git diff --check",
                f"diff_check_exit_code: {diff_check_exit_code if diff_check_exit_code is not None else 'n/a'}",
                "",
                "## stdout",
                "",
                "```text",
                stdout.strip(),
                "```",
                "",
                "## stderr",
                "",
                "```text",
                stderr.strip(),
                "```",
                "",
                "## diff check stdout",
                "",
                "```text",
                diff_check_stdout.strip(),
                "```",
                "",
                "## diff check stderr",
                "",
                "```text",
                diff_check_stderr.strip(),
                "```",
                "",
            ]
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
                "Historical partial_materialized_source:",
                "- materialized_source:",
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

    def _effective_agent_count(self, role: str, phase: str, agent_count_override: int | None = None) -> int:
        if role == "executor" and phase in SINGLE_EXECUTOR_FIX_PHASES:
            # Intentional hard rule for future AI maintainers: fix phases use one executor.
            # Multiple fix executors produce competing patches for the same defect, which
            # increases merge conflicts, context size, and risk of broad accidental changes.
            return 1
        if agent_count_override is not None:
            return agent_count_override
        return int(self.config["roles"][role]["count"])

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
            and self._artifact_visible_to(target_role, phase, artifact)
        ]
        visible_artifacts = self._filter_visible_artifacts_for_phase(visible_artifacts, phases_by_id, role, phase, round_id)
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

    def _filter_visible_artifacts_for_phase(
        self,
        artifacts: list[dict[str, Any]],
        phases_by_id: dict[str, dict[str, Any]],
        role: str,
        phase: str,
        round_id: int | None,
    ) -> list[dict[str, Any]]:
        if role != "executor" or phase != PATCH_MERGE or round_id is None:
            return artifacts

        latest_authoritative: dict[str, dict[str, Any]] = {}
        filtered: list[dict[str, Any]] = []
        candidate_patch_types = {"patch.diff", "fix_patch.diff", "patch_metadata.md"}
        authoritative_types = {
            "merged_patch.diff",
            "merged_patch_metadata.md",
            "merge_report.md",
            "patch_validation.md",
            "materialized_repo.md",
            "objective_gate.md",
            "test_gate.md",
        }
        for artifact in artifacts:
            artifact_type = artifact["artifact_type"]
            phase_row = phases_by_id.get(artifact.get("phase_id") or "")
            artifact_round = int(phase_row["round_id"]) if phase_row and phase_row.get("round_id") is not None else None
            artifact_phase = phase_row["phase_type"] if phase_row else None

            if artifact_type in candidate_patch_types:
                if artifact_round == round_id and artifact_phase in {EXECUTION, FIXING, REVIEW_FIXING}:
                    filtered.append(artifact)
                continue
            if artifact_type in authoritative_types:
                if artifact_round is not None and artifact_round < round_id:
                    latest_authoritative[artifact_type] = artifact
                continue
            filtered.append(artifact)

        filtered.extend(latest_authoritative.values())
        return filtered

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
                    "selected_plan.md",
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
                    "selected_plan.md",
                    "plan.md",
                    "assumptions.md",
                    "risk.md",
                    "todo_breakdown.md",
                    "implementation_plan.md",
                    "changed_files.md",
                    "patch.diff",
                    "patch_metadata.md",
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
                    "merged_patch_metadata.md",
                    "merge_report.md",
                }
            if phase == FIXING:
                return artifact_role in {"executor", "tester", "judge", "orchestrator"} and artifact_type in {
                    "implementation_plan.md",
                    "changed_files.md",
                    "merged_patch.diff",
                    "merged_patch_metadata.md",
                    "patch_validation.md",
                    "materialized_repo.md",
                    "objective_gate.md",
                    "test_gate.md",
                    "patch_metadata.md",
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
                    "merged_patch_metadata.md",
                    "patch_validation.md",
                    "materialized_repo.md",
                    "objective_gate.md",
                    "test_gate.md",
                    "patch_metadata.md",
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
                "merged_patch_metadata.md",
                "patch_validation.md",
                "materialized_repo.md",
                "objective_gate.md",
                "test_gate.md",
                "patch_metadata.md",
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
                "merged_patch_metadata.md",
                "patch_validation.md",
                "materialized_repo.md",
                "objective_gate.md",
                "test_gate.md",
                "patch_metadata.md",
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
                    "selected_plan.md",
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
        source_files = self._publish_materialized_source(task_id, project_dir)
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
            "selected_plan.md",
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
