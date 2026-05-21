from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from harness.agents.result import AgentRunResult
from harness.artifacts.acceptance import (
    SELECTED_PLAN_ARTIFACT,
    acceptance_oracles_from_payload,
    load_selected_plan,
    validate_acceptance_oracles,
    validate_tester_oracle_results,
)
from harness.artifacts.peer_review import (
    PEER_REVIEW_RESULT_ARTIFACT,
    load_peer_review_result,
    peer_review_code_from_payload,
)
from harness.artifacts.review_decision import REVIEW_RESULT_ARTIFACT, load_review_result, review_decision_code_from_payload
from harness.contracts.role_contracts import required_outputs_for
from harness.core.difficulty import difficulty_score_from_task_configuration, planner_peer_review_enabled_for_score
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
    TESTING,
)
from harness.core.taxonomy import RUNTIME_BLOCKER_FAILURE_TYPES
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, MISC, NEW_PROJECT
from harness.testing.tester_result import (
    ENVIRONMENT_BLOCKED,
    SOURCE_BUG,
    TESTER_RESULT_ARTIFACT,
    TESTS_PASSED,
    TesterResult,
    TesterResultError,
    load_tester_result,
)
from harness.workflow.routing import (
    WorkflowErrorType,
    WorkflowRoute,
    WorkflowRouteAction,
    choose_review_route,
    route_review_payload,
    route_tester_decision,
)
from harness.workflow.saga_adapter import BugfixSagaAdapter


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


class DeliveryService(Protocol):
    def latest_final_delivery(self, task_id: str) -> Path | None:
        ...

    def publish_delivery(self, task_id: str, final_path: Path) -> Path:
        ...


class WorkflowRuntime(PhaseRunner, GateRunner, DeliveryService, Protocol):
    config: dict[str, Any]
    repository: Any
    logger: Any
    fix_round_limit_callback: Any

    def is_failed_resume(self, task_id: str) -> bool:
        ...

    def emit_progress(self, event: ProgressEvent) -> None:
        ...

    def run_final_validation_gate(self, task_id: str, round_id: int) -> Any:
        ...


class WorkflowEngine:
    def __init__(self, runtime: WorkflowRuntime):
        self.runtime = runtime
        self.bugfix_saga = BugfixSagaAdapter(runtime)

    def run(self, task_id: str, workflow_type: str, user_prompt: str) -> Path:
        if workflow_type == BUGFIX:
            return self.run_bugfix_flow(task_id, user_prompt)
        if workflow_type == FEATURE_CHANGE:
            return self.run_feature_change_flow(task_id, user_prompt)
        if workflow_type == MISC:
            return self.run_misc_flow(task_id, user_prompt)
        return self.run_new_project_flow(task_id, user_prompt)

    def run_new_project_flow(self, task_id: str, user_prompt: str) -> Path:
        self.run_initial_planning_block(task_id, NEW_PROJECT, user_prompt)
        self.run_execution_test_loop(task_id, user_prompt)
        self.run_review_loop(task_id, user_prompt)
        self.run_final_validation_loop(task_id, user_prompt)
        return self.run_delivery(task_id, user_prompt)

    def run_bugfix_flow(self, task_id: str, user_prompt: str) -> Path:
        o = self.runtime
        max_rounds = self.max_test_fix_rounds()
        start_round = self.bugfix_resume_start_round(task_id)
        self.bugfix_saga.emit_started(task_id)
        if self.bugfix_needs_initial_planning(task_id, start_round):
            self.run_initial_planning_block(task_id, BUGFIX, user_prompt)
            self.bugfix_saga.record_route(task_id, "plan", "DecisionAccepted", phase=PLANNING_DRAFT, round_id=0)
        round_id = start_round
        attempts = 0
        fix_attempts_since_plan_recheck = 0
        while True:
            while max_rounds is None or attempts < max_rounds:
                fix_attempts_since_plan_recheck = self.maybe_run_fix_tester_plan_recheck(
                    task_id,
                    user_prompt,
                    parent_round_id=round_id - 1,
                    failed_fix_attempts=fix_attempts_since_plan_recheck,
                )
                o.run_role_phase("executor", FIXING, round_id, required_outputs_for("executor", FIXING), user_prompt)
                self.bugfix_saga.record_route(
                    task_id,
                    "execute_patch",
                    "ArtifactCanonicalized",
                    phase=FIXING,
                    round_id=round_id,
                )
                merge_ok = o.run_patch_merge(task_id, round_id, user_prompt)
                if merge_ok:
                    self.bugfix_saga.record_route(
                        task_id,
                        "materialize",
                        self.bugfix_saga.materialized_event_type(task_id, round_id),
                        phase=PATCH_MERGE,
                        round_id=round_id,
                    )
                    tester_decision = self.run_testing_until_tester_decision(task_id, TESTING, round_id, user_prompt)
                    self.bugfix_saga.record_route(
                        task_id,
                        "tester_verify",
                        self.bugfix_saga.tester_event_type(tester_decision),
                        phase=TESTING,
                        round_id=round_id,
                        payload={
                            "tester_status": tester_decision.status,
                            "failure_type": tester_decision.failure_type,
                            "artifact": str(tester_decision.artifact_path),
                        },
                    )
                    if tester_decision.tests_passed:
                        break
                    if tester_decision.environment_blocked:
                        self.raise_tester_environment_blocked(tester_decision)
                else:
                    self.bugfix_saga.record_route(
                        task_id,
                        "materialize",
                        "SnapshotInvalid",
                        phase=PATCH_MERGE,
                        round_id=round_id,
                    )
                round_id += 1
                attempts += 1
                fix_attempts_since_plan_recheck += 1
                continue
            else:
                updated_max_rounds = self.resolve_test_fix_round_limit(task_id, max_rounds)
                if updated_max_rounds == max_rounds:
                    raise TaskFailedError("Bugfix testing did not pass within max_test_fix_rounds")
                max_rounds = updated_max_rounds
                continue
            break
        self.run_review_loop(task_id, user_prompt)
        self.run_final_validation_loop(task_id, user_prompt)
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
            if phase["phase_type"] in {FIXING, PATCH_MERGE, TESTING}
            and phase["round_id"] is not None
        ]
        return max(rounds) if rounds else None

    def run_feature_change_flow(self, task_id: str, user_prompt: str) -> Path:
        self.run_initial_planning_block(task_id, FEATURE_CHANGE, user_prompt)
        self.run_execution_test_loop(task_id, user_prompt)
        self.run_review_loop(task_id, user_prompt)
        self.run_final_validation_loop(task_id, user_prompt)
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

    def run_initial_planning_block(self, task_id: str, workflow_type: str, user_prompt: str) -> None:
        if self.planning_peer_review_required(task_id, workflow_type):
            self.run_planning_block(task_id, user_prompt)
            return
        if workflow_type == BUGFIX:
            self.run_bugfix_planning_block(task_id, user_prompt)
            return
        self.run_light_planning_block(task_id, user_prompt)

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
            if self.plan_review_blocked(review_results):
                raise TaskFailedError("Planning merge review blocked the workflow")
            review_fix_mode = True
            next_round_id = final_round_id + 1
        raise TaskFailedError("Planning merge review was not approved after peer-review loops")

    def run_light_planning_block(self, task_id: str, user_prompt: str) -> None:
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
            if self.plan_review_blocked(review_results):
                raise TaskFailedError("Planning merge review blocked the workflow")
            next_round_id += 1
        raise TaskFailedError("Planning merge review was not approved")

    def run_bugfix_planning_block(self, task_id: str, user_prompt: str) -> None:
        self.run_light_planning_block(task_id, user_prompt)

    def planning_peer_review_required(self, task_id: str, workflow_type: str) -> bool:
        task = self.runtime.repository.get_task(task_id)
        score = difficulty_score_from_task_configuration(task["configuration"] if task else None)
        return planner_peer_review_enabled_for_score(self.runtime.config, workflow_type, score)

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
                if artifact.artifact_type != PEER_REVIEW_RESULT_ARTIFACT or not artifact.path.exists():
                    continue
                payload = load_peer_review_result(artifact.path)
                code = peer_review_code_from_payload(payload)
                status = str(payload.get("peer_review_status") or payload.get("status") or "").strip().lower()
                if code != 0:
                    return False
                if status in {"changes_requested", "blocked"}:
                    return False
                if status == "satisfied":
                    saw_status = True
        return saw_status

    def plan_review_approved(self, results: list[AgentRunResult]) -> bool:
        return self.plan_review_decision_code(results) == 0

    def plan_review_blocked(self, results: list[AgentRunResult]) -> bool:
        return self.plan_review_decision_code(results) == 2

    def plan_review_decision_code(self, results: list[AgentRunResult]) -> int | None:
        codes: list[int] = []
        for result in results:
            for artifact in result.artifacts:
                if artifact.artifact_type != REVIEW_RESULT_ARTIFACT or not artifact.path.exists():
                    continue
                code = review_decision_code_from_payload(load_review_result(artifact.path))
                if code is not None:
                    codes.append(code)
        for code in (2, 1, 0):
            if code in codes:
                return code
        return None

    def run_execution_test_loop(self, task_id: str, user_prompt: str) -> None:
        o = self.runtime
        max_rounds = self.max_test_fix_rounds()
        start_round = self.execution_resume_start_round(task_id)
        if start_round <= 0:
            o.run_role_phase("executor", EXECUTION, 0, required_outputs_for("executor", EXECUTION), user_prompt)
            merge_ok = o.run_patch_merge(task_id, 0, user_prompt)
            round_id = 0
            fix_attempts_since_plan_recheck = 0
        else:
            round_id = start_round
            o.run_role_phase("executor", FIXING, round_id, required_outputs_for("executor", FIXING), user_prompt)
            merge_ok = o.run_patch_merge(task_id, round_id, user_prompt)
            fix_attempts_since_plan_recheck = 0
        end_round = self.execution_test_end_round(start_round, max_rounds)
        while True:
            while end_round is None or round_id <= end_round:
                if merge_ok:
                    tester_decision = self.run_testing_until_tester_decision(task_id, TESTING, round_id, user_prompt)
                    if tester_decision.tests_passed:
                        return
                    if tester_decision.environment_blocked:
                        self.raise_tester_environment_blocked(tester_decision)
                    if round_id > 0:
                        fix_attempts_since_plan_recheck += 1
                elif round_id > 0:
                    fix_attempts_since_plan_recheck += 1
                if end_round is not None and round_id >= end_round:
                    break
                next_round = round_id + 1
                fix_attempts_since_plan_recheck = self.maybe_run_fix_tester_plan_recheck(
                    task_id,
                    user_prompt,
                    parent_round_id=round_id,
                    failed_fix_attempts=fix_attempts_since_plan_recheck,
                )
                o.run_role_phase("executor", FIXING, next_round, required_outputs_for("executor", FIXING), user_prompt)
                merge_ok = o.run_patch_merge(task_id, next_round, user_prompt)
                round_id = next_round
            updated_max_rounds = self.resolve_test_fix_round_limit(task_id, max_rounds)
            if updated_max_rounds == max_rounds:
                raise TaskFailedError("Testing did not pass within max_test_fix_rounds")
            max_rounds = updated_max_rounds
            end_round = self.execution_test_end_round(start_round, max_rounds)
            next_round = round_id + 1
            fix_attempts_since_plan_recheck = self.maybe_run_fix_tester_plan_recheck(
                task_id,
                user_prompt,
                parent_round_id=round_id,
                failed_fix_attempts=fix_attempts_since_plan_recheck,
            )
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
            if phase["phase_type"] in {EXECUTION, FIXING, PATCH_MERGE, TESTING}
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
            route = self.review_route(review_results)
            self.emit_review_route(task_id, REVIEWING, review_round_id, route)
            if route.action == WorkflowRouteAction.CONTINUE:
                break
            if route.action == WorkflowRouteAction.BLOCK_TASK:
                raise TaskFailedError(f"Reviewer blocked delivery due to {route.error_type.value}: {route.reason}")
            if route.action == WorkflowRouteAction.RETRY_ARTIFACT:
                raise TaskFailedError(f"Reviewer produced an unrouteable review_result.json: {route.reason}")
            if route.action == WorkflowRouteAction.TESTER_ENVIRONMENT_REPAIR:
                tester_decision = self.run_review_environment_repair(task_id, user_prompt, review_round_id, review_iteration)
                tester_route = route_tester_decision(tester_decision)
                if tester_route.action == WorkflowRouteAction.CONTINUE:
                    break
                if tester_route.action == WorkflowRouteAction.BLOCK_TASK:
                    raise TaskFailedError(f"Tester blocked review environment repair: {tester_route.reason}")
                if tester_route.action != WorkflowRouteAction.EXECUTOR_FIX:
                    raise TaskFailedError(f"Review environment repair did not produce a routeable result: {tester_route.reason}")

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
        return self.review_route(results).action == WorkflowRouteAction.CONTINUE

    def review_route(self, results: list[AgentRunResult]) -> WorkflowRoute:
        routes: list[WorkflowRoute] = []
        for result in results:
            for artifact in result.artifacts:
                if artifact.artifact_type != REVIEW_RESULT_ARTIFACT or not artifact.path.exists():
                    continue
                payload = load_review_result(artifact.path)
                routes.append(route_review_payload(payload))
        return choose_review_route(routes)

    def _review_verdict_json_approved(self, payload: dict[str, Any]) -> bool:
        return route_review_payload(payload).action == WorkflowRouteAction.CONTINUE

    def review_environment_block_reason(self, results: list[AgentRunResult]) -> str | None:
        route = self.review_route(results)
        if route.action == WorkflowRouteAction.BLOCK_TASK and route.error_type == WorkflowErrorType.ENVIRONMENT_BLOCKED:
            return route.reason or "reviewer reported a blocked runtime environment with no reason"
        return None

    def run_final_validation_loop(self, task_id: str, user_prompt: str) -> None:
        o = self.runtime
        max_rounds = self.max_test_fix_rounds()
        attempts = 0
        fix_attempts_since_plan_recheck = 0
        round_id = self.next_phase_round_id(task_id)
        while True:
            result = o.run_final_validation_gate(task_id, round_id)
            if getattr(result, "passed", False) or getattr(result, "skipped", False):
                return
            failure_type = str(getattr(result, "failure_type", "") or "inconclusive")
            summary = str(getattr(result, "summary", "") or "final validation failed")
            if failure_type in {"contract_bug", "contract_invalid"}:
                raise TaskFailedError(f"Final validation contract error: {summary}")
            if max_rounds is not None and attempts >= max_rounds:
                updated_max_rounds = self.resolve_test_fix_round_limit(task_id, max_rounds)
                if updated_max_rounds == max_rounds:
                    raise TaskFailedError("Final validation did not pass within max_test_fix_rounds")
                max_rounds = updated_max_rounds
                continue
            if failure_type in RUNTIME_BLOCKER_FAILURE_TYPES:
                tester_decision = self.run_testing_until_tester_decision(
                    task_id,
                    REGRESSION_TESTING,
                    round_id,
                    user_prompt,
                    phase_scope=self.phase_scope(
                        "final_validation_environment_repair",
                        parent_round_id=round_id,
                        iteration_id=attempts,
                    ),
                )
                if tester_decision.environment_blocked:
                    self.raise_tester_environment_blocked(tester_decision)
                attempts += 1
                round_id = self.next_phase_round_id(task_id)
                continue

            fix_round_id = self.next_phase_round_id(task_id)
            fix_attempts_since_plan_recheck = self.maybe_run_fix_tester_plan_recheck(
                task_id,
                user_prompt,
                parent_round_id=round_id,
                failed_fix_attempts=fix_attempts_since_plan_recheck,
            )
            fix_round_id = max(fix_round_id, self.next_phase_round_id(task_id))
            o.run_role_phase(
                "executor",
                REVIEW_FIXING,
                fix_round_id,
                required_outputs_for("executor", REVIEW_FIXING),
                user_prompt,
                phase_scope=self.phase_scope(
                    "final_validation_fix",
                    parent_round_id=round_id,
                    iteration_id=attempts,
                ),
            )
            merge_ok = o.run_patch_merge(task_id, fix_round_id, user_prompt)
            self.run_regression_test_fix_loop(task_id, user_prompt, fix_round_id, merge_ok)
            self.run_review_loop(task_id, user_prompt)
            attempts += 1
            fix_attempts_since_plan_recheck += 1
            round_id = self.next_phase_round_id(task_id)

    def run_review_environment_repair(
        self,
        task_id: str,
        user_prompt: str,
        review_round_id: int,
        review_iteration: int,
    ) -> TesterResult:
        existing = self.latest_passing_tester_decision(task_id)
        if existing:
            self.emit_review_environment_repair_reused(task_id, review_round_id, existing)
            return existing
        return self.run_testing_until_tester_decision(
            task_id,
            REGRESSION_TESTING,
            review_round_id,
            user_prompt,
            phase_scope=self.phase_scope(
                "review_environment_repair",
                parent_round_id=review_round_id,
                iteration_id=review_iteration,
            ),
        )

    def latest_passing_tester_decision(self, task_id: str) -> TesterResult | None:
        current_phase_ids = {
            str(phase["phase_id"])
            for phase in self.current_prompt_turn_phases(task_id)
            if phase["phase_type"] in {TESTING, REGRESSION_TESTING}
        }
        for artifact in reversed(self.runtime.repository.list_artifacts(task_id, TESTER_RESULT_ARTIFACT)):
            if artifact.get("phase_id") not in current_phase_ids:
                continue
            path = Path(artifact["path"])
            if not path.exists() or not path.is_file():
                continue
            try:
                decision = load_tester_result(path)
            except TesterResultError:
                continue
            if decision.tests_passed and not decision.has_environment_dependency_issue:
                return decision
        return None

    def emit_review_route(self, task_id: str, phase: str, round_id: int, route: WorkflowRoute) -> None:
        self.runtime.emit_progress(
            ProgressEvent(
                "workflow_route_decision",
                task_id=task_id,
                phase=phase,
                role="orchestrator",
                round_id=round_id,
                status=route.action.value,
                message=route.reason or f"Workflow routed to {route.action.value}",
                data={
                    "route_action": route.action.value,
                    "error_type": route.error_type.value,
                },
            )
        )

    def emit_review_environment_repair_reused(self, task_id: str, round_id: int, decision: TesterResult) -> None:
        self.runtime.emit_progress(
            ProgressEvent(
                "review_environment_repair_reused_tester_result",
                task_id=task_id,
                phase=REVIEWING,
                role="tester",
                round_id=round_id,
                status=decision.status,
                message="Reviewer requested environment follow-up; latest tester_result.json already passed without environment dependency issues.",
                data={
                    "artifact": str(decision.artifact_path),
                    "environment_dependency_issue": decision.environment_dependency_issue,
                },
            )
        )

    def run_regression_test_fix_loop(self, task_id: str, user_prompt: str, review_round_id: int, merge_ok: bool) -> None:
        o = self.runtime
        max_rounds = self.max_test_fix_rounds()
        test_round_id = 0
        fix_attempts_since_plan_recheck = 0
        while True:
            while max_rounds is None or test_round_id < max_rounds:
                phase_round_id = self.regression_phase_round_id(review_round_id, test_round_id, max_rounds)
                phase_scope = self.phase_scope(
                    "regression_test_fix",
                    parent_round_id=review_round_id,
                    iteration_id=test_round_id,
                )
                if merge_ok:
                    tester_decision = self.run_testing_until_tester_decision(
                        task_id,
                        REGRESSION_TESTING,
                        phase_round_id,
                        user_prompt,
                        phase_scope=phase_scope,
                    )
                    if tester_decision.tests_passed:
                        return
                    if tester_decision.environment_blocked:
                        self.raise_tester_environment_blocked(tester_decision)
                    fix_attempts_since_plan_recheck += 1
                else:
                    fix_attempts_since_plan_recheck += 1

                next_phase_round = max(phase_round_id + 1, self.next_phase_round_id(task_id))
                fix_attempts_since_plan_recheck = self.maybe_run_fix_tester_plan_recheck(
                    task_id,
                    user_prompt,
                    parent_round_id=phase_round_id,
                    failed_fix_attempts=fix_attempts_since_plan_recheck,
                )
                next_phase_round = max(next_phase_round, self.next_phase_round_id(task_id))
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

    def run_testing_until_tester_decision(
        self,
        task_id: str,
        phase: str,
        round_id: int,
        user_prompt: str,
        *,
        phase_scope: dict[str, int | str | None] | None = None,
    ) -> TesterResult:
        o = self.runtime
        max_repairs = self.max_tester_environment_repair_rounds()
        last_error = "missing tester_result.json"
        next_loop_type = "tester_result_retry"
        retry_feedback: list[str] | None = None
        for repair_attempt in range(max_repairs + 1):
            scoped_phase = self.tester_result_retry_scope(phase_scope, repair_attempt, loop_type=next_loop_type)
            results = o.run_role_phase(
                "tester",
                phase,
                round_id,
                required_outputs_for("tester", phase),
                user_prompt,
                phase_scope=scoped_phase,
                retry_feedback=retry_feedback,
            )
            try:
                decision = self.tester_decision_from_results(task_id, results)
            except TesterResultError as exc:
                last_error = str(exc)
                next_loop_type = "tester_result_retry"
                retry_feedback = self.tester_retry_feedback(last_error)
                self.emit_tester_result_invalid(task_id, phase, round_id, repair_attempt, last_error)
                continue
            self.emit_tester_decision(task_id, phase, round_id, decision)
            route = route_tester_decision(decision)
            if route.action == WorkflowRouteAction.TESTER_ENVIRONMENT_REPAIR:
                last_error = route.reason
                next_loop_type = "tester_environment_repair"
                retry_feedback = self.tester_environment_retry_feedback(route.reason)
                self.emit_tester_environment_repair_requested(task_id, phase, round_id, repair_attempt, decision)
                continue
            return decision
        raise TaskFailedError(
            "Tester did not clear environment dependencies or produce a valid tester_result.json "
            f"after {max_repairs + 1} attempt(s); last_error={last_error}"
        )

    def tester_decision_from_results(self, task_id: str, results: list[AgentRunResult]) -> TesterResult:
        decisions: list[TesterResult] = []
        parse_errors: list[str] = []
        selected_oracles = self._latest_acceptance_oracles(task_id)
        for result in results:
            artifact = next(
                (artifact for artifact in result.artifacts if artifact.artifact_type == TESTER_RESULT_ARTIFACT),
                None,
            )
            if artifact is None:
                parse_errors.append(f"{result.agent_id} did not produce {TESTER_RESULT_ARTIFACT}")
                continue
            try:
                decision = load_tester_result(artifact.path)
                oracle_errors = validate_tester_oracle_results(decision.payload, selected_oracles)
                if oracle_errors:
                    raise TesterResultError("; ".join(oracle_errors))
                decisions.append(decision)
            except TesterResultError as exc:
                parse_errors.append(str(exc))
        if not decisions:
            raise TesterResultError("; ".join(parse_errors) or f"missing {TESTER_RESULT_ARTIFACT}")
        environment_issues = [decision for decision in decisions if decision.has_environment_dependency_issue]
        if environment_issues:
            return environment_issues[-1]
        for status in (SOURCE_BUG, ENVIRONMENT_BLOCKED, TESTS_PASSED):
            matching = [decision for decision in decisions if decision.status == status]
            if matching:
                return matching[-1]
        raise TesterResultError(f"no recognized {TESTER_RESULT_ARTIFACT} decision")

    def _latest_acceptance_oracles(self, task_id: str):
        artifacts = self.runtime.repository.list_artifacts(task_id, SELECTED_PLAN_ARTIFACT)
        for artifact in reversed(artifacts):
            path = Path(artifact["path"])
            if not path.exists() or not path.is_file():
                continue
            try:
                payload = load_selected_plan(path)
            except ValueError as exc:
                raise TesterResultError(f"{SELECTED_PLAN_ARTIFACT} invalid: {exc}") from exc
            oracle_errors = validate_acceptance_oracles(payload)
            if oracle_errors:
                raise TesterResultError(
                    f"{SELECTED_PLAN_ARTIFACT} acceptance contract invalid: {'; '.join(oracle_errors)}"
                )
            return acceptance_oracles_from_payload(payload)
        return None

    def tester_retry_feedback(self, error: str) -> list[str]:
        return [
            "Previous tester_result.json was rejected by Harness validation: "
            f"{error}. Fix the machine-readable numeric codes and evidence; do not change only summary text."
        ]

    def tester_environment_retry_feedback(self, reason: str) -> list[str]:
        return [
            "Previous tester_result.json reported an environment dependency issue: "
            f"{reason}. Repair the environment if safe, rerun the needed verification, then update numeric codes."
        ]

    def emit_tester_result_invalid(
        self,
        task_id: str,
        phase: str,
        round_id: int,
        repair_attempt: int,
        error: str,
    ) -> None:
        self.runtime.emit_progress(
            ProgressEvent(
                "tester_result_invalid",
                task_id=task_id,
                phase=phase,
                role="tester",
                round_id=round_id,
                attempt=repair_attempt,
                status="FAILED",
                message=f"tester_result.json invalid: {error}",
            )
        )

    def emit_tester_decision(self, task_id: str, phase: str, round_id: int, decision: TesterResult) -> None:
        self.runtime.emit_progress(
            ProgressEvent(
                "tester_decision",
                task_id=task_id,
                phase=phase,
                role="tester",
                round_id=round_id,
                status=decision.status,
                message=decision.summary or f"Tester decision: {decision.status}",
                data={
                    "next_action": decision.next_action,
                    "failure_type": decision.failure_type,
                    "environment_dependency_issue": decision.environment_dependency_issue,
                    "oracle_results": list(decision.oracle_results),
                    "artifact": str(decision.artifact_path),
                },
            )
        )

    def emit_tester_environment_repair_requested(
        self,
        task_id: str,
        phase: str,
        round_id: int,
        repair_attempt: int,
        decision: TesterResult,
    ) -> None:
        self.runtime.emit_progress(
            ProgressEvent(
                "tester_environment_repair_requested",
                task_id=task_id,
                phase=phase,
                role="tester",
                round_id=round_id,
                attempt=repair_attempt,
                status="RUNNING",
                message=decision.summary or "Tester reported an environment dependency issue; rerunning tester repair loop.",
                data={
                    "failure_type": decision.failure_type,
                    "artifact": str(decision.artifact_path),
                },
            )
        )

    def raise_tester_environment_blocked(self, decision: TesterResult) -> None:
        reason = decision.summary or decision.failure_type or "tester reported environment_blocked"
        raise TaskFailedError(f"Tester blocked the task due to test environment: {reason}; artifact={decision.artifact_path}")

    def should_recheck_fix_tester_plan(self, failed_fix_attempts: int) -> bool:
        threshold = self.fix_tester_plan_recheck_after()
        return threshold is not None and failed_fix_attempts >= threshold

    def maybe_run_fix_tester_plan_recheck(
        self,
        task_id: str,
        user_prompt: str,
        *,
        parent_round_id: int | None,
        failed_fix_attempts: int,
    ) -> int:
        if not self.should_recheck_fix_tester_plan(failed_fix_attempts):
            return failed_fix_attempts
        self.run_fix_tester_plan_recheck(
            task_id,
            user_prompt,
            parent_round_id=parent_round_id,
            failed_fix_attempts=failed_fix_attempts,
        )
        return 0

    def fix_tester_plan_recheck_after(self) -> int | None:
        value = self.runtime.config.get("limits", {}).get("fix_tester_plan_recheck_after", 5)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 5
        return parsed if parsed > 0 else None

    def run_fix_tester_plan_recheck(
        self,
        task_id: str,
        user_prompt: str,
        *,
        parent_round_id: int | None,
        failed_fix_attempts: int,
    ) -> None:
        o = self.runtime
        self.emit_fix_tester_plan_recheck_requested(task_id, parent_round_id, failed_fix_attempts)
        max_rounds = max(1, int(o.config["limits"].get("max_planning_rounds", 3)))
        for iteration_id in range(max_rounds):
            round_id = self.next_phase_round_id(task_id)
            phase_scope = self.phase_scope(
                "fix_tester_plan_recheck",
                parent_round_id=parent_round_id,
                iteration_id=iteration_id,
            )
            o.run_role_phase(
                "planner",
                PLANNING_REVISION,
                round_id,
                required_outputs_for("planner", PLANNING_REVISION),
                user_prompt,
                agent_count_override=1,
                phase_scope=phase_scope,
            )
            review_results = o.run_role_phase(
                "reviewer",
                PLAN_REVIEW,
                round_id,
                required_outputs_for("reviewer", PLAN_REVIEW),
                user_prompt,
                agent_count_override=1,
                phase_scope=phase_scope,
            )
            if self.plan_review_approved(review_results):
                return
            if self.plan_review_blocked(review_results):
                raise TaskFailedError("Fix/test plan recheck was blocked by reviewer")
        raise TaskFailedError("Fix/test plan recheck was not approved")

    def emit_fix_tester_plan_recheck_requested(
        self,
        task_id: str,
        parent_round_id: int | None,
        failed_fix_attempts: int,
    ) -> None:
        self.runtime.emit_progress(
            ProgressEvent(
                "fix_tester_plan_recheck_requested",
                task_id=task_id,
                phase=PLANNING_REVISION,
                role="planner",
                round_id=parent_round_id,
                status="RUNNING",
                message=(
                    "Fix/test loop reached "
                    f"{failed_fix_attempts} failed attempt(s); asking planner to recheck the selected plan."
                ),
                data={
                    "failed_fix_attempts": failed_fix_attempts,
                    "parent_round_id": parent_round_id,
                },
            )
        )

    def tester_result_retry_scope(
        self,
        phase_scope: dict[str, int | str | None] | None,
        repair_attempt: int,
        *,
        loop_type: str = "tester_result_retry",
    ) -> dict[str, int | str | None] | None:
        if repair_attempt <= 0:
            return phase_scope
        scoped = dict(phase_scope or {})
        scoped["loop_type"] = loop_type
        scoped["iteration_id"] = repair_attempt
        return scoped

    def max_tester_environment_repair_rounds(self) -> int:
        value = self.runtime.config.get("limits", {}).get("max_tester_environment_repair_rounds", 2)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 2

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
            raise TaskFailedError("Communicator did not produce final_delivery.json")
        return o.publish_delivery(task_id, final_path)
