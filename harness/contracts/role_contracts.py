from __future__ import annotations

from dataclasses import dataclass

from harness.artifacts.schemas import (
    RolePhaseContract,
    output_contract_lines_for as schema_output_contract_lines_for,
    required_outputs_for as schema_required_outputs_for,
    role_phase_contract_for as schema_role_phase_contract_for,
)
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, MISC, NEW_PROJECT, normalize_workflow_type


DEFAULT_ROLE_INSTRUCTIONS = {
    "planner": (
        "Create planning artifacts only. Analyze the request, existing artifacts, assumptions, risks, "
        "compatibility constraints, and an actionable task breakdown. Do not modify source files. "
        "delivery.md is a JSON role return envelope. It must be exactly one JSON object with "
        "`return_code` set to `0` when you produced the required planning files, even if you identify high risks. "
        "Complete planning Markdown artifacts must contain `artifact_result_code: 0`."
    ),
    "executor": (
        "Create the artifacts required by the current executor phase. For implementation and fix phases, "
        "express code changes as unified diff files and supporting notes. For miscellaneous response phases, "
        "answer the request without modifying project files. Do not decide workflow progression or communicate "
        "with the user outside required artifacts. delivery.md is a JSON role return envelope. It must be exactly one "
        "JSON object with `return_code` set to `0` when you produced the required files, regardless of the "
        "implementation complexity. Complete executor Markdown artifacts must contain `artifact_result_code: 0`."
    ),
    "tester": (
        "Evaluate executor artifacts and available repository state. Produce a single bug_report.md "
        "with explicit build, test, and bug verdicts plus reproducible evidence. "
        "IMPORTANT: delivery.md is a JSON role return envelope, not the test verdict. It must be exactly one "
        "JSON object with `return_code` set to `0` as long as you completed the evaluation and produced the required report, "
        "even if the test verdict is `test_result_code: -1` or you find critical bugs. "
        "`artifact_result_code` must be `0` for a complete tester report; put build/test/bug outcomes only in "
        "`build_result_code`, `test_result_code`, and `bug_result_code` inside bug_report.md."
    ),
    "reviewer": (
        "Review the final executor implementation for correctness, scope control, regressions, maintainability, "
        "and customer-machine runtime readiness. When this is a code delivery, run the repository on the current machine, "
        "attempt local isolated dependency setup when needed, and verify the delivered environment actually works. "
        "If runtime issues are fixable, request changes in `review_report.md`; if the runtime or system conflict is irreconcilable, "
        "report a blocked environment through the required JSON section in `review_report.md`. delivery.md is a JSON role return envelope. "
        "It must be exactly one JSON object with `return_code` set to `0` if you completed the review, regardless of whether "
        "the review verdict is `review_decision_code: 0`, `review_decision_code: 1`, or `review_decision_code: -1`. "
        "`review_report.md` must contain `artifact_result_code: 0` when complete."
    ),
    "judge": (
        "Make the phase decision from collected artifacts only. Produce a strict machine-readable decision "
        "and a concise rationale. Do not create implementation changes. delivery.md is a JSON role return envelope, "
        "not the phase verdict. It must be exactly one JSON object with `return_code` set to `0` if you rendered a "
        "clear decision, even when `decision.json` contains `decision: fail` or `decision: changes_required`. "
        "`decision_summary.md` must contain `artifact_result_code: 0` when complete."
    ),
    "communicator": (
        "Create customer-facing delivery artifacts only. Use the accepted plan and final executor implementation to "
        "describe what was built, how it works, and how the customer should run it. delivery.md is a JSON role return envelope. "
        "It must be exactly one JSON object with `return_code` set to `0` if the final delivery documentation is complete. "
        "`final_delivery.md` and `usage_guide.md` must contain `artifact_result_code: 0` when complete."
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


def role_contract_for(workflow_type: str | None, role: str, phase: str) -> RoleContract:
    phase_contract = role_phase_contract_for(role, phase)
    required_outputs = tuple(phase_contract.required_outputs_with_delivery())
    return RoleContract(
        workflow_type=normalize_workflow_type(workflow_type or NEW_PROJECT),
        role=role,
        phase=phase,
        role_instruction=role_instruction_for(role, workflow_type),
        phase_contract=phase_contract,
        required_outputs=required_outputs,
        output_contract_lines=tuple(output_contract_lines_for(role, phase, list(required_outputs))),
    )
