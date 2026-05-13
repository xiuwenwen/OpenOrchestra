from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from harness.agents.result import AgentRunResult
from harness.contracts.role_contracts import required_outputs_for
from harness.core.errors import TaskFailedError
from harness.core.progress import ProgressEvent
from harness.core.state_machine import (
    DELIVERY,
    EXECUTION,
    FAILED,
    FIXING,
    MISC_RESPONSE,
    PATCH_MERGE,
    PLAN_REVIEW,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEWING,
    TEST_JUDGEMENT,
    TESTING,
)
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, MISC


class PhaseRunner(Protocol):
    def run_role_phase(
        self,
        role: str,
        phase: str,
        round_id: int,
        required_outputs: list[str],
        user_prompt: str | None = None,
        agent_count_override: int | None = None,
        phase_scope: dict[str, int | str | None] | None = None,
    ) -> list[AgentRunResult]:
        ...


class GateRunner(Protocol):
    def run_patch_merge(self, task_id: str, round_id: int, user_prompt: str) -> bool:
        ...

    def run_harness_test_gate(self, task_id: str, round_id: int) -> bool:
        ...

    def run_judge_phase(
        self,
        task_id: str,
        phase: str,
        round_id: int,
        user_prompt: str,
        phase_scope: dict[str, int | str | None] | None = None,
    ) -> dict[str, Any]:
        ...


class DeliveryService(Protocol):
    def latest_final_delivery(self, task_id: str) -> Path | None:
        ...

    def publish_delivery(self, task_id: str, final_path: Path) -> Path:
        ...


class WorkflowRuntime(PhaseRunner, GateRunner, DeliveryService, Protocol):
    config: dict[str, Any]
    repository: Any
    logger: Any
    judge: Any
    fix_round_limit_callback: Any

    def is_failed_resume(self, task_id: str) -> bool:
        ...

    def emit_progress(self, event: ProgressEvent) -> None:
        ...


class WorkflowEngine:
    def __init__(self, runtime: WorkflowRuntime):
        self.runtime = runtime

    def run(self, task_id: str, workflow_type: str, user_prompt: str) -> Path:
        if workflow_type == BUGFIX:
            return self.run_bugfix_flow(task_id, user_prompt)
        if workflow_type == FEATURE_CHANGE:
            return self.run_feature_change_flow(task_id, user_prompt)
        if workflow_type == MISC:
            return self.run_misc_flow(task_id, user_prompt)
        return self.run_new_project_flow(task_id, user_prompt)

    def run_new_project_flow(self, task_id: str, user_prompt: str) -> Path:
        self.run_planning_block(task_id, user_prompt)
        self.run_execution_test_loop(task_id, user_prompt)
        self.run_review_loop(task_id, user_prompt)
        return self.run_delivery(task_id, user_prompt)

    def run_bugfix_flow(self, task_id: str, user_prompt: str) -> Path:
        o = self.runtime
        max_rounds = self.max_test_fix_rounds()
        start_round = self.bugfix_resume_start_round(task_id)
        if self.bugfix_needs_initial_planning(task_id, start_round):
            self.run_bugfix_planning_block(task_id, user_prompt)
        round_id = start_round
        attempts = 0
        while True:
            while max_rounds is None or attempts < max_rounds:
                o.run_role_phase("executor", FIXING, round_id, required_outputs_for("executor", FIXING), user_prompt)
                merge_ok = o.run_patch_merge(task_id, round_id, user_prompt)
                if merge_ok:
                    o.run_harness_test_gate(task_id, round_id)
                    o.run_role_phase("tester", TESTING, round_id, required_outputs_for("tester", TESTING), user_prompt)
                    test_decision = o.run_judge_phase(task_id, TEST_JUDGEMENT, round_id, user_prompt)
                    if o.judge.is_test_pass(test_decision):
                        break
                round_id += 1
                attempts += 1
                continue
            else:
                updated_max_rounds = self.resolve_test_fix_round_limit(task_id, max_rounds)
                if updated_max_rounds == max_rounds:
                    raise TaskFailedError("Bugfix testing did not pass within max_test_fix_rounds")
                max_rounds = updated_max_rounds
                continue
            if o.judge.is_test_pass(test_decision):
                break
        self.run_review_loop(task_id, user_prompt)
        return self.run_delivery(task_id, user_prompt)

    def bugfix_needs_initial_planning(self, task_id: str, start_round: int) -> bool:
        if start_round > 0:
            return False
        planning_phases = {PLANNING_DRAFT, PLANNING_PEER_REVIEW, PLANNING_REVISION, PLAN_REVIEW}
        return not any(phase["phase_type"] in planning_phases for phase in self.current_prompt_turn_phases(task_id))

    def bugfix_resume_start_round(self, task_id: str) -> int:
        o = self.runtime
        if not o.is_failed_resume(task_id):
            return 0
        highest_round = self.highest_bugfix_round_id(task_id)
        if highest_round is None:
            return 0
        max_rounds = self.max_test_fix_rounds()
        if max_rounds is None:
            return highest_round + 1
        if highest_round + 1 < max_rounds:
            return 0
        return highest_round + 1

    def highest_bugfix_round_id(self, task_id: str) -> int | None:
        rounds = [
            int(phase["round_id"])
            for phase in self.current_prompt_turn_phases(task_id)
            if phase["phase_type"] in {FIXING, PATCH_MERGE, TESTING, TEST_JUDGEMENT}
            and phase["round_id"] is not None
        ]
        return max(rounds) if rounds else None

    def run_feature_change_flow(self, task_id: str, user_prompt: str) -> Path:
        self.run_planning_block(task_id, user_prompt)
        self.run_execution_test_loop(task_id, user_prompt)
        self.run_review_loop(task_id, user_prompt)
        return self.run_delivery(task_id, user_prompt)

    def run_misc_flow(self, task_id: str, user_prompt: str) -> Path:
        o = self.runtime
        o.run_role_phase(
            "executor",
            MISC_RESPONSE,
            0,
            required_outputs_for("executor", MISC_RESPONSE),
            user_prompt,
            agent_count_override=1,
        )
        artifacts = o.repository.list_artifacts(task_id, "response.md")
        if not artifacts:
            raise TaskFailedError("Misc workflow executor did not produce response.md")
        return Path(artifacts[-1]["path"])

    def run_planning_block(self, task_id: str, user_prompt: str) -> None:
        o = self.runtime
        planner_count = int(o.effective_agent_count(task_id, "planner", PLANNING_DRAFT))
        loop_count = self.planning_peer_review_loop_count()
        effective_loop_count = loop_count if planner_count > 1 else 1
        next_round_id = 0
        review_fix_mode = False
        for approval_round in range(int(o.config["limits"]["max_planning_rounds"])):
            final_round_id = next_round_id
            if review_fix_mode:
                o.run_role_phase(
                    "planner",
                    PLANNING_REVISION,
                    final_round_id,
                    required_outputs_for("planner", PLANNING_REVISION),
                    user_prompt,
                )
            else:
                for loop_round in range(effective_loop_count):
                    round_id = next_round_id
                    final_round_id = round_id
                    phase = PLANNING_DRAFT if round_id == 0 else PLANNING_REVISION
                    o.run_role_phase("planner", phase, round_id, required_outputs_for("planner", phase), user_prompt)
                    if planner_count <= 1:
                        break
                    peer_results = o.run_role_phase(
                        "planner",
                        PLANNING_PEER_REVIEW,
                        round_id,
                        required_outputs_for("planner", PLANNING_PEER_REVIEW),
                        user_prompt,
                    )
                    if self.peer_reviews_satisfied(peer_results):
                        break
                    next_round_id = round_id + 1
            review_results = o.run_role_phase(
                "reviewer",
                PLAN_REVIEW,
                final_round_id,
                required_outputs_for("reviewer", PLAN_REVIEW),
                user_prompt,
                agent_count_override=1,
            )
            if self.plan_review_approved(review_results):
                return
            review_fix_mode = True
            next_round_id = final_round_id + 1
        raise TaskFailedError("Planning merge review was not approved after peer-review loops")

    def run_bugfix_planning_block(self, task_id: str, user_prompt: str) -> None:
        o = self.runtime
        planner_count = int(o.effective_agent_count(task_id, "planner", PLANNING_DRAFT))
        next_round_id = 0
        for approval_round in range(int(o.config["limits"]["max_planning_rounds"])):
            phase = PLANNING_DRAFT if approval_round == 0 else PLANNING_REVISION
            o.run_role_phase(
                "planner",
                phase,
                next_round_id,
                required_outputs_for("planner", phase),
                user_prompt,
                agent_count_override=planner_count,
            )
            review_results = o.run_role_phase(
                "reviewer",
                PLAN_REVIEW,
                next_round_id,
                required_outputs_for("reviewer", PLAN_REVIEW),
                user_prompt,
                agent_count_override=1,
            )
            if self.plan_review_approved(review_results):
                return
            next_round_id += 1
        raise TaskFailedError("Bugfix planning merge review was not approved")

    def planning_peer_review_loop_count(self) -> int:
        configured = self.runtime.config.get("limits", {}).get(
            "planning_peer_review_loops",
            self.runtime.config.get("limits", {}).get("max_planning_rounds", 3),
        )
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            return 3

    def peer_reviews_satisfied(self, results: list[AgentRunResult]) -> bool:
        if not results:
            return False
        saw_status = False
        for result in results:
            for artifact in result.artifacts:
                if artifact.artifact_type != "peer_review.md" or not artifact.path.exists():
                    continue
                text = artifact.path.read_text(encoding="utf-8", errors="replace").lower()
                if re.search(r"(?m)^\s*peer_review_code\s*:\s*(-?1|2|3|-2|-3)\s*$", text):
                    return False
                if re.search(r"(?m)^\s*peer_review_code\s*:\s*0\s*$", text):
                    saw_status = True
                if "peer_review_status: changes_requested" in text or "status: changes_requested" in text:
                    return False
                if "peer_review_status: satisfied" in text or "status: satisfied" in text:
                    saw_status = True
        return saw_status

    def plan_review_approved(self, results: list[AgentRunResult]) -> bool:
        for result in results:
            for artifact in result.artifacts:
                if artifact.artifact_type != "review_report.md" or not artifact.path.exists():
                    continue
                text = artifact.path.read_text(encoding="utf-8", errors="replace").lower()
                if re.search(r"(?m)^\s*review_decision_code\s*:\s*0\s*$", text):
                    return True
        return False

    def run_execution_test_loop(self, task_id: str, user_prompt: str) -> None:
        o = self.runtime
        max_rounds = self.max_test_fix_rounds()
        start_round = self.execution_resume_start_round(task_id)
        if start_round <= 0:
            o.run_role_phase("executor", EXECUTION, 0, required_outputs_for("executor", EXECUTION), user_prompt)
            merge_ok = o.run_patch_merge(task_id, 0, user_prompt)
            round_id = 0
        else:
            round_id = start_round
            o.run_role_phase("executor", FIXING, round_id, required_outputs_for("executor", FIXING), user_prompt)
            merge_ok = o.run_patch_merge(task_id, round_id, user_prompt)
        end_round = self.execution_test_end_round(start_round, max_rounds)
        while True:
            while end_round is None or round_id <= end_round:
                if merge_ok:
                    o.run_harness_test_gate(task_id, round_id)
                    o.run_role_phase("tester", TESTING, round_id, required_outputs_for("tester", TESTING), user_prompt)
                    test_decision = o.run_judge_phase(task_id, TEST_JUDGEMENT, round_id, user_prompt)
                    if o.judge.is_test_pass(test_decision):
                        return
                if end_round is not None and round_id >= end_round:
                    break
                next_round = round_id + 1
                o.run_role_phase("executor", FIXING, next_round, required_outputs_for("executor", FIXING), user_prompt)
                merge_ok = o.run_patch_merge(task_id, next_round, user_prompt)
                round_id = next_round
            updated_max_rounds = self.resolve_test_fix_round_limit(task_id, max_rounds)
            if updated_max_rounds == max_rounds:
                raise TaskFailedError("Testing did not pass within max_test_fix_rounds")
            max_rounds = updated_max_rounds
            end_round = self.execution_test_end_round(start_round, max_rounds)
            next_round = round_id + 1
            o.run_role_phase("executor", FIXING, next_round, required_outputs_for("executor", FIXING), user_prompt)
            merge_ok = o.run_patch_merge(task_id, next_round, user_prompt)
            round_id = next_round

    def execution_test_end_round(self, start_round: int, max_rounds: int | None) -> int | None:
        if max_rounds is None:
            return None
        if start_round <= 0:
            return max_rounds
        return start_round + max(1, max_rounds) - 1

    def execution_resume_start_round(self, task_id: str) -> int:
        o = self.runtime
        if not o.is_failed_resume(task_id):
            return 0
        highest_round = self.highest_execution_test_round_id(task_id)
        if highest_round is None:
            return 0
        max_rounds = self.max_test_fix_rounds()
        if max_rounds is None:
            return highest_round + 1
        if highest_round < max_rounds:
            return 0
        return highest_round + 1

    def max_test_fix_rounds(self) -> int | None:
        value = self.runtime.config.get("limits", {}).get("max_test_fix_rounds", 10)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"", "0", "-1", "none", "no_limit", "nolimit", "infinite", "infinity", "unlimited"}:
                return None
            parsed = int(normalized)
        else:
            parsed = int(value)
        return parsed if parsed > 0 else None

    def resolve_test_fix_round_limit(self, task_id: str, current_limit: int | None) -> int | None:
        o = self.runtime
        if current_limit is None:
            return None
        message = f"[WARN] 已达最大修复轮次({current_limit})，任务终止。"
        o.logger.warning(message)
        o.emit_progress(
            ProgressEvent(
                "test_fix_round_limit_reached",
                task_id=task_id,
                status=FAILED,
                message=message,
                data={
                    "max_test_fix_rounds": current_limit,
                    "choices": ["extra_10", "exit", "unlimited"],
                },
            )
        )
        if not o.fix_round_limit_callback:
            return current_limit
        choice = o.fix_round_limit_callback(task_id, current_limit).strip().lower()
        if choice in {"extra_10", "10", "+10", "continue", "继续", "额外给10轮"}:
            return current_limit + 10
        if choice in {"unlimited", "fix_until_done", "fix_until_fixed", "until_fixed", "一直修复", "fix直至修复"}:
            return None
        return current_limit

    def highest_execution_test_round_id(self, task_id: str) -> int | None:
        rounds = [
            int(phase["round_id"])
            for phase in self.current_prompt_turn_phases(task_id)
            if phase["phase_type"] in {EXECUTION, FIXING, PATCH_MERGE, TESTING, TEST_JUDGEMENT}
            and phase["round_id"] is not None
        ]
        return max(rounds) if rounds else None

    def run_review_loop(self, task_id: str, user_prompt: str) -> None:
        o = self.runtime
        review_round_id = 0
        for review_iteration in range(o.config["limits"]["max_review_rounds"]):
            o.run_runtime_readiness_gate(task_id, review_round_id)
            review_results = o.run_role_phase(
                "reviewer",
                REVIEWING,
                review_round_id,
                required_outputs_for("reviewer", REVIEWING),
                user_prompt,
                phase_scope=self.phase_scope("review", iteration_id=review_iteration),
            )
            blocked_reason = self.review_environment_block_reason(review_results)
            if blocked_reason:
                raise TaskFailedError(f"Reviewer blocked delivery due to runtime environment conflict: {blocked_reason}")
            if self.review_approved(review_results):
                break

            fix_round_id = self.next_phase_round_id(task_id)
            o.run_role_phase(
                "executor",
                REVIEW_FIXING,
                fix_round_id,
                required_outputs_for("executor", REVIEW_FIXING),
                user_prompt,
                phase_scope=self.phase_scope("review_fix", parent_round_id=review_round_id, iteration_id=0),
            )
            merge_ok = o.run_patch_merge(task_id, fix_round_id, user_prompt)
            self.run_regression_test_fix_loop(task_id, user_prompt, fix_round_id, merge_ok)
            review_round_id = self.next_phase_round_id(task_id)
        else:
            raise TaskFailedError("Review was not approved within max_review_rounds")

    def review_approved(self, results: list[AgentRunResult]) -> bool:
        for result in results:
            for artifact in result.artifacts:
                if artifact.artifact_type != "review_report.md" or not artifact.path.exists():
                    continue
                text = artifact.path.read_text(encoding="utf-8", errors="replace")
                if not re.search(r"(?m)^\s*review_decision_code\s*:\s*0\s*$", text):
                    continue
                payload = self._extract_review_verdict_json(text)
                if self._review_verdict_json_approved(payload):
                    return True
        return False

    def _review_verdict_json_approved(self, payload: dict[str, Any]) -> bool:
        status = str(
            payload.get("review_status")
            or payload.get("decision")
            or payload.get("status")
            or ""
        ).strip().lower()
        if status not in {"approved", "approve", "pass", "passed", "success"}:
            return False
        environment_check = payload.get("environment_check")
        if not isinstance(environment_check, dict):
            return False
        environment_status = str(environment_check.get("status") or "").strip().lower()
        return environment_status in {"ready", "not_applicable", "pass", "passed", "success"}

    def review_environment_block_reason(self, results: list[AgentRunResult]) -> str | None:
        for result in results:
            for artifact in result.artifacts:
                if artifact.artifact_type != "review_report.md" or not artifact.path.exists():
                    continue
                payload = self._extract_review_verdict_json(artifact.path.read_text(encoding="utf-8", errors="replace"))
                environment_check = payload.get("environment_check")
                if not isinstance(environment_check, dict):
                    continue
                status = str(environment_check.get("status") or "").strip().lower()
                if status != "blocked":
                    continue
                reason = environment_check.get("blocking_reason") or payload.get("blocking_reason") or payload.get("reason")
                text = str(reason).strip()
                return text or "reviewer reported a blocked runtime environment with no reason"
        return None

    def _extract_review_verdict_json(self, content: str) -> dict[str, Any]:
        marker = "## Review Verdict JSON"
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

    def run_regression_test_fix_loop(self, task_id: str, user_prompt: str, review_round_id: int, merge_ok: bool) -> None:
        o = self.runtime
        max_rounds = self.max_test_fix_rounds()
        test_round_id = 0
        while True:
            while max_rounds is None or test_round_id < max_rounds:
                phase_round_id = self.regression_phase_round_id(review_round_id, test_round_id, max_rounds)
                phase_scope = self.phase_scope(
                    "regression_test_fix",
                    parent_round_id=review_round_id,
                    iteration_id=test_round_id,
                )
                if merge_ok:
                    o.run_harness_test_gate(task_id, phase_round_id)
                    o.run_role_phase(
                        "tester",
                        REGRESSION_TESTING,
                        phase_round_id,
                        required_outputs_for("tester", REGRESSION_TESTING),
                        user_prompt,
                        phase_scope=phase_scope,
                    )
                    test_decision = o.run_judge_phase(task_id, TEST_JUDGEMENT, phase_round_id, user_prompt)
                    if o.judge.is_test_pass(test_decision):
                        return

                next_phase_round = max(phase_round_id + 1, self.next_phase_round_id(task_id))
                o.run_role_phase(
                    "executor",
                    REVIEW_FIXING,
                    next_phase_round,
                    required_outputs_for("executor", REVIEW_FIXING),
                    user_prompt,
                    phase_scope=self.phase_scope(
                        "regression_test_fix",
                        parent_round_id=review_round_id,
                        iteration_id=test_round_id + 1,
                    ),
                )
                merge_ok = o.run_patch_merge(task_id, next_phase_round, user_prompt)
                test_round_id += 1
            updated_max_rounds = self.resolve_test_fix_round_limit(task_id, max_rounds)
            if updated_max_rounds == max_rounds:
                raise TaskFailedError("Regression testing did not pass within max_test_fix_rounds")
            max_rounds = updated_max_rounds

    def regression_phase_round_id(self, review_round_id: int, test_round_id: int, max_rounds: int | None) -> int:
        return review_round_id + test_round_id

    def next_phase_round_id(self, task_id: str) -> int:
        existing_rounds = [
            int(phase["round_id"])
            for phase in self.current_prompt_turn_phases(task_id)
            if phase["round_id"] is not None
        ]
        return max(existing_rounds, default=-1) + 1

    def current_prompt_turn_phases(self, task_id: str) -> list[dict[str, Any]]:
        task = self.runtime.repository.get_task(task_id)
        prompt_turn_id = int(task["prompt_turn_id"] or 0) if task else 0
        return [
            phase
            for phase in self.runtime.repository.list_phases(task_id)
            if int(phase["prompt_turn_id"] or 0) == prompt_turn_id
        ]

    def phase_scope(
        self,
        loop_type: str,
        *,
        parent_round_id: int | None = None,
        iteration_id: int | None = None,
    ) -> dict[str, int | str | None]:
        return {
            "loop_type": loop_type,
            "parent_round_id": parent_round_id,
            "iteration_id": iteration_id,
        }

    def run_delivery(self, task_id: str, user_prompt: str) -> Path:
        o = self.runtime
        o.run_role_phase("communicator", DELIVERY, 0, required_outputs_for("communicator", DELIVERY), user_prompt)
        final_path = o.latest_final_delivery(task_id)
        if not final_path:
            raise TaskFailedError("Communicator did not produce final_delivery.md")
        return o.publish_delivery(task_id, final_path)
