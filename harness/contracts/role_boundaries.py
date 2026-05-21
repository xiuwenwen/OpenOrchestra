from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleBoundary:
    role: str
    responsibility: str
    source_access: str
    command_authority: str
    environment_authority: str
    owned_outputs: tuple[str, ...]
    forbidden_actions: tuple[str, ...]


ROLE_BOUNDARIES: dict[str, RoleBoundary] = {
    "planner": RoleBoundary(
        role="planner",
        responsibility="Turn the request and evidence into plans, risks, environment contracts, validation contracts, and executor-ready todos.",
        source_access="read-only; no implementation edits",
        command_authority="read-only inspection commands only when needed; no workflow, gate, install, or source-modifying commands",
        environment_authority="describe required environment facts and unknowns; do not repair the runtime environment",
        owned_outputs=("plan artifacts", "todo_breakdown.json", "environment_contract_draft.json", "validation_contract_draft.json"),
        forbidden_actions=("modifying source files", "declaring test verdicts", "deciding delivery approval"),
    ),
    "executor": RoleBoundary(
        role="executor",
        responsibility="Implement or fix source behavior according to the selected plan and structured gate feedback.",
        source_access="writable for implementation phases; changes must be represented by required patch artifacts",
        command_authority="may run local build/self-check commands needed to implement safely, but must not replace tester verdicts",
        environment_authority="may use the provided runtime; does not own dependency/setup failure classification",
        owned_outputs=("patch.diff", "fix_patch.diff", "implementation notes", "self_check.md"),
        forbidden_actions=("marking tests passed for Harness routing", "treating environment blockers as source bugs", "updating Harness state"),
    ),
    "tester": RoleBoundary(
        role="tester",
        responsibility="Own environment setup, test execution, oracle verification, and the structured test verdict.",
        source_access="read-only for implementation source; may write isolated runtime/test state only",
        command_authority="must run safe setup/build/test/smoke commands required by the contracts when possible",
        environment_authority="owns environment repair loop and must classify unresolved dependency/setup blockers",
        owned_outputs=("bug_report.md", "tester_result.json"),
        forbidden_actions=("modifying implementation source", "sending environment blockers to executor as source bugs", "using executor prose as test evidence"),
    ),
    "reviewer": RoleBoundary(
        role="reviewer",
        responsibility="Review plan or implementation artifacts against contracts, tester evidence, runtime readiness, and acceptance oracles.",
        source_access="read-only; may create isolated verification state only",
        command_authority="may run verification commands for review evidence; must not mutate implementation source",
        environment_authority="records runtime-readiness findings; routes fixable environment issues back to tester-owned repair",
        owned_outputs=("review_result.json", "selected_plan.json", "environment_contract.json", "validation_contract.json"),
        forbidden_actions=("dropping compatible acceptance requirements", "requesting source changes for pure environment blockers", "writing review_report.md"),
    ),
    "communicator": RoleBoundary(
        role="communicator",
        responsibility="Produce the final customer-facing delivery summary and usage guide from accepted artifacts.",
        source_access="read-only",
        command_authority="read-only inspection only; do not perform implementation or validation work",
        environment_authority="summarize confirmed setup/run commands; do not repair environment",
        owned_outputs=("final_delivery.json", "usage_guide.md"),
        forbidden_actions=("modifying source files", "changing test verdicts", "padding handoff with unsupported claims"),
    ),
}


def role_boundary_for(role: str) -> RoleBoundary:
    try:
        return ROLE_BOUNDARIES[role]
    except KeyError:
        return RoleBoundary(
            role=role,
            responsibility="Follow the current phase contract.",
            source_access="as allowed by the phase contract",
            command_authority="as allowed by the phase contract",
            environment_authority="as allowed by the phase contract",
            owned_outputs=(),
            forbidden_actions=("updating Harness state directly",),
        )


def role_boundary_prompt_lines_for(role: str) -> list[str]:
    boundary = role_boundary_for(role)
    return [
        f"- Responsibility: {boundary.responsibility}",
        f"- Source access: {boundary.source_access}",
        f"- Command authority: {boundary.command_authority}",
        f"- Environment authority: {boundary.environment_authority}",
        "- Owned outputs: " + (", ".join(f"`{item}`" for item in boundary.owned_outputs) if boundary.owned_outputs else "phase-required outputs"),
        "- Forbidden actions: " + ", ".join(boundary.forbidden_actions),
    ]
