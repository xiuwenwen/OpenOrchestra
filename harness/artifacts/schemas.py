from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.core.state_machine import (
    DELIVERY,
    EXECUTION,
    FIXING,
    MISC_RESPONSE,
    PATCH_MERGE,
    PLAN_REVIEW,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEW_JUDGEMENT,
    REVIEWING,
    TEST_JUDGEMENT,
    TESTING,
)


ANY_PHASE = "*"
ROUND_ANY = "any"
ROUND_CURRENT = "current"
ROUND_PREVIOUS = "previous"
ROUND_BEFORE_CURRENT = "before_current"
ROUND_LATEST_PER_TYPE = "latest_per_type"
ROUND_LATEST_BEFORE_CURRENT_PER_TYPE = "latest_before_current_per_type"
ROUND_LATEST_COMPLETE_TEST_BEFORE_CURRENT = "latest_complete_test_before_current"
ROUND_LATEST_COMPLETE_JUDGE_BEFORE_CURRENT = "latest_complete_judge_before_current"
ROUND_LATEST_PLANNING = "latest_planning_round"
ROUND_REJECTED_PLAN_REVIEW = "latest_rejected_plan_review"
SELF_INCLUDE = "include"
SELF_EXCLUDE = "exclude"
CONDITION_ALWAYS = "always"
CONDITION_NO_REJECTED_PLAN_REVIEW = "no_rejected_plan_review"
CONDITION_HAS_REJECTED_PLAN_REVIEW = "has_rejected_plan_review"


@dataclass(frozen=True)
class ArtifactVisibilityRule:
    target_role: str
    target_phase: str
    source_role: str
    artifact_types: frozenset[str]
    source_phases: frozenset[str] | None = None
    round_policy: str = ROUND_ANY
    self_policy: str = SELF_INCLUDE
    condition: str = CONDITION_ALWAYS


@dataclass(frozen=True)
class RolePhaseContract:
    role: str
    phase: str
    required_outputs: tuple[str, ...]
    visibility_rules: tuple[ArtifactVisibilityRule, ...] = ()

    def required_outputs_with_delivery(self) -> list[str]:
        outputs = list(self.required_outputs)
        if DELIVERY_STATUS_OUTPUT not in outputs:
            outputs.append(DELIVERY_STATUS_OUTPUT)
        return outputs


def _types(*artifact_types: str) -> frozenset[str]:
    return frozenset(artifact_types)


def _phases(*phases: str) -> frozenset[str]:
    return frozenset(phases)


PLANNING_ARTIFACTS = _types("plan.md", "assumptions.md", "risk.md", "todo_breakdown.md")
PROJECT_CONTEXT_ARTIFACTS = _types("project_context.md")
TEST_REPORT_ARTIFACTS = _types("bug_report.md")
JUDGE_DECISION_ARTIFACTS = _types("decision.json", "decision_summary.md")
PATCH_FIX_GATE_ARTIFACTS = _types("objective_gate.md")
TEST_JUDGE_GATE_ARTIFACTS = _types("test_gate.md", "objective_gate.md")
EXECUTOR_REVIEW_ARTIFACTS = _types(
    "changed_files.md",
    "merged_patch.diff",
    "merged_patch_metadata.md",
    "self_check.md",
)
DELIVERY_EXECUTOR_ARTIFACTS = _types(
    "changed_files.md",
    "merged_patch_metadata.md",
    "self_check.md",
)
ARTIFACT_VISIBILITY_RULES: tuple[ArtifactVisibilityRule, ...] = (
    ArtifactVisibilityRule("planner", PLANNING_DRAFT, "context", PROJECT_CONTEXT_ARTIFACTS),
    ArtifactVisibilityRule("planner", PLANNING_REVISION, "context", PROJECT_CONTEXT_ARTIFACTS),
    ArtifactVisibilityRule("reviewer", PLAN_REVIEW, "context", PROJECT_CONTEXT_ARTIFACTS),
    ArtifactVisibilityRule("executor", EXECUTION, "context", PROJECT_CONTEXT_ARTIFACTS),
    ArtifactVisibilityRule("executor", FIXING, "context", PROJECT_CONTEXT_ARTIFACTS),
    ArtifactVisibilityRule("executor", REVIEW_FIXING, "context", PROJECT_CONTEXT_ARTIFACTS),
    ArtifactVisibilityRule("executor", MISC_RESPONSE, "context", PROJECT_CONTEXT_ARTIFACTS),
    ArtifactVisibilityRule("planner", PLANNING_DRAFT, "planner", PLANNING_ARTIFACTS, round_policy=ROUND_BEFORE_CURRENT),
    ArtifactVisibilityRule("planner", PLANNING_DRAFT, "judge", JUDGE_DECISION_ARTIFACTS, round_policy=ROUND_BEFORE_CURRENT),
    ArtifactVisibilityRule(
        "planner",
        PLANNING_PEER_REVIEW,
        "planner",
        PLANNING_ARTIFACTS,
        source_phases=_phases(PLANNING_DRAFT, PLANNING_REVISION),
        round_policy=ROUND_CURRENT,
        self_policy=SELF_EXCLUDE,
    ),
    ArtifactVisibilityRule(
        "planner",
        PLANNING_REVISION,
        "planner",
        PLANNING_ARTIFACTS | _types("peer_review.md"),
        round_policy=ROUND_PREVIOUS,
        condition=CONDITION_NO_REJECTED_PLAN_REVIEW,
    ),
    ArtifactVisibilityRule(
        "planner",
        PLANNING_REVISION,
        "judge",
        JUDGE_DECISION_ARTIFACTS,
        round_policy=ROUND_PREVIOUS,
        condition=CONDITION_NO_REJECTED_PLAN_REVIEW,
    ),
    ArtifactVisibilityRule(
        "planner",
        PLANNING_REVISION,
        "reviewer",
        _types("review_report.md"),
        source_phases=_phases(PLAN_REVIEW),
        round_policy=ROUND_REJECTED_PLAN_REVIEW,
        condition=CONDITION_HAS_REJECTED_PLAN_REVIEW,
    ),
    ArtifactVisibilityRule(
        "reviewer",
        PLAN_REVIEW,
        "planner",
        PLANNING_ARTIFACTS | _types("peer_review.md"),
        source_phases=_phases(PLANNING_DRAFT, PLANNING_REVISION, PLANNING_PEER_REVIEW),
        round_policy=ROUND_CURRENT,
    ),
    ArtifactVisibilityRule(
        "executor",
        EXECUTION,
        "reviewer",
        _types("selected_plan.md"),
        source_phases=_phases(PLAN_REVIEW),
        round_policy=ROUND_LATEST_PLANNING,
    ),
    ArtifactVisibilityRule(
        "executor",
        PATCH_MERGE,
        "executor",
        _types("patch.diff", "fix_patch.diff"),
        source_phases=_phases(EXECUTION, FIXING, REVIEW_FIXING),
        round_policy=ROUND_CURRENT,
    ),
    ArtifactVisibilityRule(
        "executor",
        PATCH_MERGE,
        "executor",
        _types("merged_patch.diff", "merged_patch_metadata.md"),
        source_phases=_phases(PATCH_MERGE),
        round_policy=ROUND_LATEST_BEFORE_CURRENT_PER_TYPE,
    ),
    ArtifactVisibilityRule("executor", FIXING, "orchestrator", PATCH_FIX_GATE_ARTIFACTS, round_policy=ROUND_PREVIOUS),
    ArtifactVisibilityRule(
        "executor",
        FIXING,
        "executor",
        _types("merged_patch_metadata.md"),
        source_phases=_phases(PATCH_MERGE),
        round_policy=ROUND_PREVIOUS,
    ),
    ArtifactVisibilityRule(
        "executor",
        FIXING,
        "tester",
        TEST_REPORT_ARTIFACTS,
        source_phases=_phases(TESTING, REGRESSION_TESTING),
        round_policy=ROUND_LATEST_COMPLETE_TEST_BEFORE_CURRENT,
    ),
    ArtifactVisibilityRule(
        "executor",
        FIXING,
        "judge",
        JUDGE_DECISION_ARTIFACTS,
        source_phases=_phases(TEST_JUDGEMENT, REVIEW_JUDGEMENT),
        round_policy=ROUND_LATEST_COMPLETE_JUDGE_BEFORE_CURRENT,
    ),
    ArtifactVisibilityRule("executor", REVIEW_FIXING, "orchestrator", PATCH_FIX_GATE_ARTIFACTS, round_policy=ROUND_PREVIOUS),
    ArtifactVisibilityRule(
        "executor",
        REVIEW_FIXING,
        "executor",
        _types("merged_patch_metadata.md"),
        source_phases=_phases(PATCH_MERGE),
        round_policy=ROUND_PREVIOUS,
    ),
    ArtifactVisibilityRule(
        "executor",
        REVIEW_FIXING,
        "tester",
        TEST_REPORT_ARTIFACTS,
        source_phases=_phases(TESTING, REGRESSION_TESTING),
        round_policy=ROUND_LATEST_COMPLETE_TEST_BEFORE_CURRENT,
    ),
    ArtifactVisibilityRule(
        "executor",
        REVIEW_FIXING,
        "judge",
        JUDGE_DECISION_ARTIFACTS,
        source_phases=_phases(TEST_JUDGEMENT, REVIEW_JUDGEMENT),
        round_policy=ROUND_LATEST_COMPLETE_JUDGE_BEFORE_CURRENT,
    ),
    ArtifactVisibilityRule(
        "reviewer",
        REVIEWING,
        "reviewer",
        _types("selected_plan.md"),
        source_phases=_phases(PLAN_REVIEW),
        round_policy=ROUND_LATEST_PER_TYPE,
    ),
    ArtifactVisibilityRule("reviewer", REVIEWING, "executor", EXECUTOR_REVIEW_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("judge", TEST_JUDGEMENT, "orchestrator", TEST_JUDGE_GATE_ARTIFACTS, round_policy=ROUND_CURRENT),
    ArtifactVisibilityRule(
        "judge",
        TEST_JUDGEMENT,
        "tester",
        TEST_REPORT_ARTIFACTS,
        source_phases=_phases(TESTING, REGRESSION_TESTING),
        round_policy=ROUND_CURRENT,
    ),
    ArtifactVisibilityRule(
        "judge",
        REVIEW_JUDGEMENT,
        "executor",
        _types("merged_patch_metadata.md"),
        source_phases=_phases(PATCH_MERGE),
        round_policy=ROUND_LATEST_PER_TYPE,
    ),
    ArtifactVisibilityRule(
        "judge",
        REVIEW_JUDGEMENT,
        "reviewer",
        _types("review_report.md"),
        source_phases=_phases(REVIEWING),
        round_policy=ROUND_CURRENT,
    ),
    ArtifactVisibilityRule(
        "communicator",
        DELIVERY,
        "reviewer",
        _types("selected_plan.md"),
        source_phases=_phases(PLAN_REVIEW),
        round_policy=ROUND_LATEST_PER_TYPE,
    ),
    ArtifactVisibilityRule("communicator", DELIVERY, "executor", DELIVERY_EXECUTOR_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
)

DELIVERY_STATUS_OUTPUT = "delivery.md"


ROLE_PHASE_CONTRACTS: dict[tuple[str, str], RolePhaseContract] = {}


def _contract(role: str, phase: str, outputs: tuple[str, ...]) -> RolePhaseContract:
    contract = RolePhaseContract(
        role=role,
        phase=phase,
        required_outputs=outputs,
        visibility_rules=tuple(rule for rule in ARTIFACT_VISIBILITY_RULES if rule.target_role == role and rule.target_phase in {phase, ANY_PHASE}),
    )
    ROLE_PHASE_CONTRACTS[(role, phase)] = contract
    return contract


_contract("planner", PLANNING_DRAFT, ("plan.md", "assumptions.md", "risk.md", "todo_breakdown.md"))
_contract("planner", PLANNING_PEER_REVIEW, ("peer_review.md",))
_contract("planner", PLANNING_REVISION, ("plan.md", "assumptions.md", "risk.md", "todo_breakdown.md"))
_contract("executor", EXECUTION, ("implementation_plan.md", "changed_files.md", "patch.diff", "self_check.md"))
_contract("executor", PATCH_MERGE, ("merged_patch.diff", "merged_patch_metadata.md", "merge_report.md"))
_contract("executor", MISC_RESPONSE, ("response.md", "notes.md"))
_contract("executor", FIXING, ("fix_schedule.md", "fix_patch.diff", "fix_notes.md", "self_check.md"))
_contract("executor", REVIEW_FIXING, ("fix_schedule.md", "fix_patch.diff", "fix_notes.md", "self_check.md"))
_contract("tester", TESTING, ("bug_report.md",))
_contract("tester", REGRESSION_TESTING, ("bug_report.md",))
_contract("reviewer", PLAN_REVIEW, ("review_report.md", "selected_plan.md"))
_contract("reviewer", REVIEWING, ("review_report.md",))
_contract("judge", TEST_JUDGEMENT, ("decision.json", "decision_summary.md"))
_contract("judge", REVIEW_JUDGEMENT, ("decision.json", "decision_summary.md"))
_contract("communicator", DELIVERY, ("final_delivery.md", "usage_guide.md"))


REQUIRED_OUTPUTS: dict[str, dict[str, list[str]] | list[str]] = {
    "planner": {
        PLANNING_DRAFT: list(ROLE_PHASE_CONTRACTS[("planner", PLANNING_DRAFT)].required_outputs),
        PLANNING_PEER_REVIEW: list(ROLE_PHASE_CONTRACTS[("planner", PLANNING_PEER_REVIEW)].required_outputs),
        PLANNING_REVISION: list(ROLE_PHASE_CONTRACTS[("planner", PLANNING_REVISION)].required_outputs),
    },
    "executor": {
        EXECUTION: list(ROLE_PHASE_CONTRACTS[("executor", EXECUTION)].required_outputs),
        PATCH_MERGE: list(ROLE_PHASE_CONTRACTS[("executor", PATCH_MERGE)].required_outputs),
        MISC_RESPONSE: list(ROLE_PHASE_CONTRACTS[("executor", MISC_RESPONSE)].required_outputs),
        FIXING: list(ROLE_PHASE_CONTRACTS[("executor", FIXING)].required_outputs),
        REVIEW_FIXING: list(ROLE_PHASE_CONTRACTS[("executor", REVIEW_FIXING)].required_outputs),
    },
    "tester": list(ROLE_PHASE_CONTRACTS[("tester", TESTING)].required_outputs),
    "reviewer": {
        PLAN_REVIEW: list(ROLE_PHASE_CONTRACTS[("reviewer", PLAN_REVIEW)].required_outputs),
        REVIEWING: list(ROLE_PHASE_CONTRACTS[("reviewer", REVIEWING)].required_outputs),
    },
    "judge": list(ROLE_PHASE_CONTRACTS[("judge", TEST_JUDGEMENT)].required_outputs),
    "communicator": list(ROLE_PHASE_CONTRACTS[("communicator", DELIVERY)].required_outputs),
}


def role_phase_contract_for(role: str, phase: str) -> RolePhaseContract:
    contract = ROLE_PHASE_CONTRACTS.get((role, phase))
    if contract:
        return contract
    spec = REQUIRED_OUTPUTS[role]
    outputs = tuple(spec[phase] if isinstance(spec, dict) else spec)
    return RolePhaseContract(role=role, phase=phase, required_outputs=outputs)


def required_outputs_for(role: str, phase: str) -> list[str]:
    return role_phase_contract_for(role, phase).required_outputs_with_delivery()


def output_contract_lines_for(role: str, phase: str, required_outputs: list[str]) -> list[str]:
    markdown_outputs = [
        name
        for name in required_outputs
        if name != DELIVERY_STATUS_OUTPUT and name.endswith(".md")
    ]
    markdown_list = ", ".join(f"`{name}`" for name in markdown_outputs)
    base = [
        "- Every role and every phase must create `delivery.md`.",
        "- `delivery.md` is the JSON role return envelope, not the task/business verdict.",
        "- `delivery.md` must be exactly one JSON object with no Markdown, prose, code fence, YAML, table, or bullet text.",
        '- Required JSON shape: `{"return_code":0,"task_status":"success","role_return_code":0,"produced_files":["delivery.md"],"known_risks":[]}`.',
        "- JSON `return_code` must be `0` when the required role files are complete.",
        "- Do not copy phase verdict values into `return_code`.",
    ]
    if markdown_outputs:
        verb = "must contain" if len(markdown_outputs) == 1 else "must each contain"
        base.append(f"- {markdown_list} {verb} `artifact_result_code: 0` somewhere in the file when complete.")
        base.append("- Do not use `artifact_result_code` to report a negative phase verdict.")
    if role == "tester":
        return [
            *base,
            "- Do not use `artifact_result_code` to report build failure, test failure, blocked tests, or blocking bugs.",
            "- Do not copy `build_result_code`, `test_result_code`, or `bug_result_code` values into `artifact_result_code` or `return_code`.",
            "- Put build outcome in `bug_report.md` as `build_result_code: 0`, `build_result_code: -1`, or `build_result_code: 2`.",
            "- Put test outcome in `bug_report.md` as `test_result_code: 0`, `test_result_code: -1`, or `test_result_code: 2`.",
            "- Put bug outcome in `bug_report.md` as `bug_result_code: 0`, `bug_result_code: 1`, or `bug_result_code: -1`.",
            "- If testing is blocked by a broken implementation, still write a complete `bug_report.md` with `artifact_result_code: 0` and describe the blocker in the verdict fields.",
            "- Harness validates `delivery.md` and report headers; any non-zero `return_code` or non-zero `artifact_result_code` prevents the run from advancing.",
        ]
    if role == "reviewer":
        return [
            *base,
            "- Put review outcome only in `review_report.md` as `review_decision_code: 0`, `review_decision_code: 1`, or `review_decision_code: -1`.",
            "- Do not copy `review_decision_code` into `artifact_result_code` or `return_code`.",
            "- `review_report.md` must also include a `## Review Verdict JSON` section with one fenced `json` object describing runtime/environment verification.",
            '- Required JSON keys: `review_status`, `environment_check.attempted`, `environment_check.status`, `environment_check.commands_run`, `environment_check.fixable`, and `environment_check.blocking_reason`.',
            "- Use `environment_check.status: blocked` only for irreconcilable runtime or system conflicts that should stop Harness immediately.",
        ]
    if role == "judge":
        return [
            *base,
            "- Put the phase verdict only in `decision.json.decision`.",
            "- Put the numeric summary only in `decision_summary.md` as `decision_code: 0`, `decision_code: 1`, or `decision_code: -1` according to the phase rules below.",
            "- Do not copy `decision_code` or `decision.json.decision` into `artifact_result_code` or `return_code`.",
        ]
    if role == "planner":
        return [
            *base,
            "- For `peer_review.md`, put peer-review outcome only in `peer_review_code: 0`, `peer_review_code: 1`, or `peer_review_code: -1`.",
            "- Do not copy `peer_review_code` into `artifact_result_code` or `return_code`.",
        ]
    if role == "communicator":
        return [
            *base,
            "- Put final delivery outcome only in `final_delivery.md` as `final_delivery_code: 0`, `final_delivery_code: 1`, `final_delivery_code: 2`, or `final_delivery_code: -1`.",
            "- Do not copy `final_delivery_code` into `artifact_result_code` or `return_code`.",
        ]
    return [
        *base,
        "- For executor Markdown notes and metadata, use `artifact_result_code: 0` only to mean the file is complete.",
        "- Do not use `artifact_result_code` or `return_code` to report implementation quality, patch validity, review verdicts, or test verdicts.",
    ]
