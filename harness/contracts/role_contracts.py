from __future__ import annotations

from dataclasses import dataclass

from harness.artifacts.schemas import (
    RolePhaseContract,
    output_contract_lines_for as schema_output_contract_lines_for,
    required_outputs_for as schema_required_outputs_for,
    role_phase_contract_for as schema_role_phase_contract_for,
)
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
    REVIEWING,
    TESTING,
)
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, MISC, NEW_PROJECT, normalize_workflow_type


@dataclass(frozen=True)
class ArtifactInputBudget:
    max_files: int = 50
    max_file_bytes: int = 262_144
    max_total_bytes: int = 1_048_576
    large_artifact_mode: str = "auto"


DEFAULT_ARTIFACT_INPUT_BUDGET = ArtifactInputBudget()
ROLE_PHASE_INPUT_BUDGETS: dict[tuple[str, str], ArtifactInputBudget] = {
    ("planner", PLANNING_DRAFT): ArtifactInputBudget(max_files=32, max_file_bytes=131_072, max_total_bytes=786_432),
    ("planner", PLANNING_PEER_REVIEW): ArtifactInputBudget(max_files=32, max_file_bytes=131_072, max_total_bytes=786_432),
    ("planner", PLANNING_REVISION): ArtifactInputBudget(max_files=32, max_file_bytes=131_072, max_total_bytes=786_432),
    ("executor", EXECUTION): ArtifactInputBudget(max_files=12, max_file_bytes=262_144, max_total_bytes=786_432),
    ("executor", PATCH_MERGE): ArtifactInputBudget(max_files=8, max_file_bytes=262_144, max_total_bytes=786_432),
    ("executor", MISC_RESPONSE): ArtifactInputBudget(max_files=8, max_file_bytes=131_072, max_total_bytes=524_288),
    ("executor", FIXING): ArtifactInputBudget(max_files=12, max_file_bytes=131_072, max_total_bytes=524_288, large_artifact_mode="truncated"),
    ("executor", REVIEW_FIXING): ArtifactInputBudget(max_files=12, max_file_bytes=131_072, max_total_bytes=524_288, large_artifact_mode="truncated"),
    ("tester", TESTING): ArtifactInputBudget(max_files=8, max_file_bytes=131_072, max_total_bytes=524_288, large_artifact_mode="path_only"),
    ("tester", REGRESSION_TESTING): ArtifactInputBudget(max_files=8, max_file_bytes=131_072, max_total_bytes=524_288, large_artifact_mode="path_only"),
    ("reviewer", PLAN_REVIEW): ArtifactInputBudget(max_files=32, max_file_bytes=131_072, max_total_bytes=786_432),
    ("reviewer", REVIEWING): ArtifactInputBudget(max_files=12, max_file_bytes=131_072, max_total_bytes=524_288, large_artifact_mode="truncated"),
    ("communicator", DELIVERY): ArtifactInputBudget(max_files=8, max_file_bytes=131_072, max_total_bytes=524_288, large_artifact_mode="path_only"),
}


DEFAULT_ROLE_INSTRUCTIONS = {
    "planner": (
        "Create planning artifacts only. Analyze the request, existing artifacts, assumptions, risks, "
        "compatibility constraints, environment/setup requirements, validation requirements, and an actionable task breakdown. "
        "Fill the Harness-created environment and validation contract draft JSON files; when the prompt or artifacts do not provide enough information, mark the relevant mode as unknown instead of inventing default commands. Do not modify source files. "
        "delivery.md is a JSON role return envelope. It must be exactly one JSON object with "
        "`return_code` set to `0` when you produced the required planning files, even if you identify high risks. "
        "Complete planning Markdown artifacts must contain `artifact_result_code: 0`."
    ),
    "executor": (
        "Create the artifacts required by the current executor phase. For implementation and fix phases, "
        "express code changes as unified diff files and supporting notes. For miscellaneous response phases, "
        "answer the request without modifying project files. If the current repository already satisfies the requested fix and staged tester/reviewer evidence shows no source bug, treat this as a valid no-op fix: do not recreate a historical patch, produce the required artifacts, write an empty fix_patch.diff when no additional current-baseline changes are needed, and record the no-op evidence in fix notes and self-check. "
        "Do not decide workflow progression or communicate "
        "with the user outside required artifacts. delivery.md is a JSON role return envelope. It must be exactly one "
        "JSON object with `return_code` set to `0` when you produced the required files, regardless of the "
        "implementation complexity. Complete executor Markdown artifacts must contain `artifact_result_code: 0`."
    ),
    "tester": (
        "Evaluate executor artifacts and available repository state. Produce bug_report.md "
        "with explicit build, test, and bug verdicts plus reproducible evidence, and produce "
        "tester_result.json as the strict workflow decision. Tester owns test environment preparation and command execution: "
        "use `environment_contract.json` and `validation_contract.json` when present, run their declared or discoverable setup/test commands in an isolated environment or configured test runtime when safe, repair environment failures, and rerun the relevant command before reporting tests as blocked. "
        "If a contract is missing or invalid when one is expected, report `failure_type: contract_bug`; do not silently fall back to default pytest. "
        "Only use project-declared dependencies or reasonable minimal test tooling such as pytest/coverage/tox/nox/build tooling; do not install broad arbitrary packages just to make tests run. "
        "Do not rely on a later Harness test gate; `tester_result.json` must decide `tests_passed`, `source_bug`, or `environment_blocked`. "
        "`tester_result.json.environment_dependency_issue` must explicitly report whether dependency/setup/build/test execution is blocked by environment problems; Harness checks it before `status`. "
        "When `selected_plan.json.acceptance_oracles` is present, verify those oracle IDs directly and fill `tester_result.json.oracle_results`; do not reinterpret the acceptance baseline from executor prose. "
        "Do not declare a fix correct when build or test execution is blocked, and do not send environment/setup failures to executor as source bugs. "
        "IMPORTANT: delivery.md is a JSON role return envelope, not the test verdict. It must be exactly one "
        "JSON object with `return_code` set to `0` as long as you completed the evaluation and produced the required report, "
        "even if the test verdict is `test_result_code: -1` or you find critical bugs. "
        "`artifact_result_code` must be `0` for complete tester artifacts; put build/test/bug outcomes only in "
        "`build_result_code`, `test_result_code`, and `bug_result_code` inside bug_report.md."
    ),
    "reviewer": (
        "Review the final executor implementation for correctness, scope control, regressions, maintainability, "
        "and customer-machine runtime readiness. When this is a code delivery, run the repository on the current machine, "
        "use Harness runtime-readiness evidence when present, attempt local isolated dependency setup when needed, and verify the delivered environment actually works. "
        "When tester_result.json is present, treat it as the structured test verdict and reconcile it with runtime-readiness evidence before requesting source changes. "
        "During plan review, extract acceptance semantics from every available proposal, review artifact, and candidate report before selecting implementation work. "
        "Also merge every planner's environment and validation contract draft into final `environment_contract.json` and `validation_contract.json`; these contracts are downstream source of truth. "
        "Do not equate plan selection with acceptance selection: preserve every compatible acceptance requirement from any proposal in `selected_plan.json.acceptance_oracles`, even when the implementation strategy that introduced it is not selected. "
        "If acceptance requirements conflict, record the conflict in `review_result.json.required_changes` instead of silently choosing one. "
        "Fill the Harness-created `review_result.json` template with the review verdict and environment evidence; do not create `review_report.md`. "
        "delivery.md is a JSON role return envelope. "
        "It must be exactly one JSON object with `return_code` set to `0` if you completed the review, regardless of whether "
        "the review verdict is `review_decision_code: 0`, `review_decision_code: 1`, or `review_decision_code: 2`."
    ),
    "communicator": (
        "Create customer-facing delivery artifacts only. Use the accepted plan, final executor implementation, "
        "and final tester report/result to describe what was built, how it works, and how the customer should run it. "
        "delivery.md is a JSON role return envelope. "
        "It must be exactly one JSON object with `return_code` set to `0` if the final delivery documentation is complete. "
        "`final_delivery.json` must contain the machine-readable delivery summary; `usage_guide.md` contains the operator guide."
    ),
}

ROLE_INSTRUCTIONS_BY_WORKFLOW = {
    NEW_PROJECT: DEFAULT_ROLE_INSTRUCTIONS,
    BUGFIX: DEFAULT_ROLE_INSTRUCTIONS,
    FEATURE_CHANGE: DEFAULT_ROLE_INSTRUCTIONS,
    MISC: DEFAULT_ROLE_INSTRUCTIONS,
}


@dataclass(frozen=True)
class RoleContract:
    workflow_type: str
    role: str
    phase: str
    role_instruction: str
    phase_contract: RolePhaseContract
    input_budget: ArtifactInputBudget
    required_outputs: tuple[str, ...]
    output_contract_lines: tuple[str, ...]


def role_instruction_for(role: str, workflow_type: str | None = None) -> str:
    normalized_workflow = normalize_workflow_type(workflow_type or NEW_PROJECT)
    instructions = ROLE_INSTRUCTIONS_BY_WORKFLOW.get(normalized_workflow, DEFAULT_ROLE_INSTRUCTIONS)
    return instructions.get(role, DEFAULT_ROLE_INSTRUCTIONS.get(role, ""))


def role_phase_contract_for(role: str, phase: str) -> RolePhaseContract:
    return schema_role_phase_contract_for(role, phase)


def required_outputs_for(role: str, phase: str) -> list[str]:
    return schema_required_outputs_for(role, phase)


def output_contract_lines_for(role: str, phase: str, required_outputs: list[str]) -> list[str]:
    return schema_output_contract_lines_for(role, phase, required_outputs)


def artifact_input_budget_for(role: str, phase: str) -> ArtifactInputBudget:
    return ROLE_PHASE_INPUT_BUDGETS.get((role, phase), DEFAULT_ARTIFACT_INPUT_BUDGET)


def role_contract_for(workflow_type: str | None, role: str, phase: str) -> RoleContract:
    phase_contract = role_phase_contract_for(role, phase)
    required_outputs = tuple(phase_contract.required_outputs_with_delivery())
    return RoleContract(
        workflow_type=normalize_workflow_type(workflow_type or NEW_PROJECT),
        role=role,
        phase=phase,
        role_instruction=role_instruction_for(role, workflow_type),
        phase_contract=phase_contract,
        input_budget=artifact_input_budget_for(role, phase),
        required_outputs=required_outputs,
        output_contract_lines=tuple(output_contract_lines_for(role, phase, list(required_outputs))),
    )
