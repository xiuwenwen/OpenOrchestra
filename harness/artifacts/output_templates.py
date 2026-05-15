from __future__ import annotations

import json
import re
from pathlib import Path


TEMPLATE_STATUS_FIELD = "harness_template_status"
TEMPLATE_PENDING_VALUE = "pending_model_completion"
TEMPLATE_PENDING_LINE = f"{TEMPLATE_STATUS_FIELD}: {TEMPLATE_PENDING_VALUE}"
TEMPLATE_PENDING_LINE_PATTERN = re.compile(
    rf"^\s*{re.escape(TEMPLATE_STATUS_FIELD)}\s*:\s*{re.escape(TEMPLATE_PENDING_VALUE)}\s*$",
    re.MULTILINE,
)


def seed_output_templates(
    output_dir: Path,
    required_outputs: list[str],
    *,
    role: str,
    phase: str,
    agent_id: str,
) -> list[Path]:
    """Create editable output templates for non-diff required files.

    The templates intentionally contain a pending marker. A model must replace
    the body and remove that marker before validation can pass.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    seeded: list[Path] = []
    for relative_name in required_outputs:
        if relative_name.endswith(".diff"):
            continue
        path = output_dir / relative_name
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            output_template_content(relative_name, required_outputs, role=role, phase=phase, agent_id=agent_id),
            encoding="utf-8",
        )
        seeded.append(path)
    return seeded


def output_template_content(
    relative_name: str,
    required_outputs: list[str],
    *,
    role: str,
    phase: str,
    agent_id: str,
) -> str:
    if relative_name == "delivery.md":
        return _delivery_template(required_outputs, role=role, phase=phase, agent_id=agent_id)
    if relative_name == "decision.json":
        return _decision_template(role=role, phase=phase, agent_id=agent_id)
    if relative_name == "tester_result.json":
        return _tester_result_template()
    if relative_name == "review_result.json":
        return _review_result_template()
    if relative_name == "peer_review_result.json":
        return _peer_review_result_template()
    if relative_name == "todo_breakdown.json":
        return _todo_breakdown_template()
    if relative_name == "selected_plan.json":
        return _selected_plan_template()
    if relative_name in {"environment_contract_draft.json", "environment_contract.json"}:
        return _environment_contract_template(relative_name)
    if relative_name in {"validation_contract_draft.json", "validation_contract.json"}:
        return _validation_contract_template(relative_name)
    if relative_name == "merged_patch_metadata.json":
        return _merged_patch_metadata_template()
    if relative_name == "final_delivery.json":
        return _final_delivery_template()
    if relative_name.endswith(".md"):
        return _markdown_template(relative_name, role=role, phase=phase, agent_id=agent_id)
    return f"{TEMPLATE_PENDING_LINE}\n\nReplace this Harness output template with `{relative_name}` content.\n"


def output_has_pending_template_marker(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.name.endswith(".json") or path.name == "delivery.md":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if _json_contains_pending_template_value(payload):
            return True
    return bool(TEMPLATE_PENDING_LINE_PATTERN.search(text))


def _delivery_template(required_outputs: list[str], *, role: str, phase: str, agent_id: str) -> str:
    return (
        json.dumps(
            {
                "return_code": 0,
                "task_status": "success",
                "role_return_code": 0,
                "role": role,
                "phase": phase,
                "agent_id": agent_id,
                "produced_files": list(required_outputs),
                "known_risks": [],
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _decision_template(*, role: str, phase: str, agent_id: str) -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "decision_code": TEMPLATE_PENDING_VALUE,
                "decision": TEMPLATE_PENDING_VALUE,
                "summary": "Replace this Harness output template with the judge decision summary.",
                "reason": "Replace this Harness output template with the actual judge decision reason.",
                "evidence": [],
                "role": role,
                "phase": phase,
                "agent_id": agent_id,
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _tester_result_template() -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "status": TEMPLATE_PENDING_VALUE,
                "next_action": TEMPLATE_PENDING_VALUE,
                "failure_type": TEMPLATE_PENDING_VALUE,
                "environment_dependency_issue": TEMPLATE_PENDING_VALUE,
                "summary": "Replace this Harness output template with the tester decision.",
                "setup_commands_run": [],
                "test_commands_run": [],
                "oracle_results": [
                    {
                        "oracle_id": TEMPLATE_PENDING_VALUE,
                        "status": TEMPLATE_PENDING_VALUE,
                        "evidence": "Replace with concrete command/static evidence for this oracle.",
                        "commands_run": [],
                        "output_excerpt": "",
                    }
                ],
                "remaining_blockers": [],
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _review_result_template() -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "review_decision_code": TEMPLATE_PENDING_VALUE,
                "review_decision_code_meaning": {
                    "0": "approved_continue",
                    "1": "changes_required",
                    "2": "blocked",
                },
                "summary": "Replace this Harness output template with the reviewer decision summary.",
                "findings": [],
                "required_changes": [],
                "acceptance_oracle_changes": [],
                "environment_check": {
                    "attempted": TEMPLATE_PENDING_VALUE,
                    "status": TEMPLATE_PENDING_VALUE,
                    "commands_run": [],
                    "fixable": TEMPLATE_PENDING_VALUE,
                    "blocking_reason": "",
                },
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _peer_review_result_template() -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "peer_review_code": TEMPLATE_PENDING_VALUE,
                "peer_review_status": TEMPLATE_PENDING_VALUE,
                "summary": "Replace this Harness output template with the peer-review summary.",
                "findings": [],
                "required_changes": [],
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _todo_breakdown_template() -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "todos": [
                    {
                        "id": TEMPLATE_PENDING_VALUE,
                        "title": TEMPLATE_PENDING_VALUE,
                        "owner_role": "executor",
                        "status": "pending",
                        "acceptance_criteria": [],
                    }
                ],
                "risks": [],
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _selected_plan_template() -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "selected_plan_id": TEMPLATE_PENDING_VALUE,
                "summary": "Replace this Harness output template with the selected plan summary.",
                "environment_contract_id": "",
                "validation_contract_id": "",
                "source_artifacts": [],
                "execution_order": [],
                "acceptance_criteria": [],
                "required_executor_notes": [],
                "reviewer_integrated_findings": [],
                "risks": [],
                "acceptance_oracles": [
                    {
                        "id": TEMPLATE_PENDING_VALUE,
                        "description": "Replace with the user-visible behavior to verify.",
                        "kind": "runtime",
                        "required": True,
                        "commands": [],
                        "expected_exception": "",
                        "must_contain": [],
                        "must_not_contain": [],
                        "semantic_assertions": [],
                        "failure_signal": "Replace with the observable failure that means this oracle failed.",
                        "evidence_hint": "Replace with the exact evidence tester should capture.",
                    }
                ],
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _environment_contract_template(relative_name: str) -> str:
    is_draft = relative_name.endswith("_draft.json")
    return (
        json.dumps(
            {
                "schema_version": "environment_contract.v1",
                "contract_id": "environment-draft" if is_draft else "environment-final",
                "contract_status": "draft" if is_draft else "final",
                "source": "planner" if is_draft else "plan_review",
                "confidence": TEMPLATE_PENDING_VALUE,
                "runtime": {
                    "type": TEMPLATE_PENDING_VALUE,
                    "language": "",
                    "version": "",
                    "base_commit": "",
                    "environment_setup_commit": "",
                },
                "setup": {
                    # `mode` carries the meaning; an empty command list alone is not a decision.
                    "mode": TEMPLATE_PENDING_VALUE,
                    "commands": [],
                    "discovery_allowed": True,
                    "notes": "",
                },
                "dependencies": {
                    "mode": TEMPLATE_PENDING_VALUE,
                    "commands": [],
                    "files": [],
                    "notes": "",
                },
                "constraints": {
                    "forbidden_validation_methods": [],
                },
                "unknowns": [],
                "evidence_sources": [],
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _validation_contract_template(relative_name: str) -> str:
    is_draft = relative_name.endswith("_draft.json")
    return (
        json.dumps(
            {
                "schema_version": "validation_contract.v1",
                "contract_id": "validation-draft" if is_draft else "validation-final",
                "contract_status": "draft" if is_draft else "final",
                "source": "planner" if is_draft else "plan_review",
                "confidence": TEMPLATE_PENDING_VALUE,
                "runtime": TEMPLATE_PENDING_VALUE,
                "tests": {
                    # `mode` carries the meaning; an empty command list alone is not a decision.
                    "mode": TEMPLATE_PENDING_VALUE,
                    "commands": [],
                    "discovery_allowed": True,
                    "fail_to_pass": [],
                    "pass_to_pass": [],
                    "notes": "",
                },
                "pass_criteria": {
                    "type": TEMPLATE_PENDING_VALUE,
                    "conditions": [],
                    "resolved": None,
                },
                "acceptance_oracle_ids": [],
                "unknowns": [],
                "evidence_sources": [],
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _merged_patch_metadata_template() -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "patch_artifact": "merged_patch.diff",
                "round_id": TEMPLATE_PENDING_VALUE,
                "base_round": TEMPLATE_PENDING_VALUE,
                "base_task_id": TEMPLATE_PENDING_VALUE,
                "base_source_type": TEMPLATE_PENDING_VALUE,
                "base_source_path": TEMPLATE_PENDING_VALUE,
                "apply_target": TEMPLATE_PENDING_VALUE,
                "patch_scope": "merged_authoritative",
                "changed_files": [],
                "expected_apply_command": "git apply --whitespace=nowarn merged_patch.diff",
                "compatibility_notes": "",
                "merge_report": {
                    "merge_strategy": TEMPLATE_PENDING_VALUE,
                    "selected_candidate_artifacts": [],
                    "rejected_candidate_artifacts": [],
                    "conflict_handling": TEMPLATE_PENDING_VALUE,
                    "ready_for_testing": TEMPLATE_PENDING_VALUE,
                },
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _final_delivery_template() -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "final_delivery_code": TEMPLATE_PENDING_VALUE,
                "status": TEMPLATE_PENDING_VALUE,
                "summary": "Replace this Harness output template with the final delivery summary.",
                "delivered_artifacts": [],
                "verification": [],
                "known_risks": [],
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _json_contains_pending_template_value(value: object) -> bool:
    if value == TEMPLATE_PENDING_VALUE:
        return True
    if isinstance(value, dict):
        return any(_json_contains_pending_template_value(child) for child in value.values())
    if isinstance(value, list):
        return any(_json_contains_pending_template_value(child) for child in value)
    return False


def _markdown_template(relative_name: str, *, role: str, phase: str, agent_id: str) -> str:
    title = relative_name.removesuffix(".md").replace("_", " ").title()
    return (
        "artifact_result_code: 0\n"
        f"{TEMPLATE_PENDING_LINE}\n\n"
        f"# {title}\n\n"
        f"Harness pre-created this template for `{role}` `{phase}` `{agent_id}`.\n\n"
        "Replace this text with the completed deliverable content, keep the exact "
        "`artifact_result_code: 0` line, and remove the `harness_template_status` "
        "line before exiting.\n"
    )
