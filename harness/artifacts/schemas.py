from __future__ import annotations

DELIVERY_STATUS_OUTPUT = "delivery.md"


REQUIRED_OUTPUTS: dict[str, dict[str, list[str]] | list[str]] = {
    "planner": {
        "PLANNING_DRAFT": ["plan.md", "assumptions.md", "risk.md", "todo_breakdown.md"],
        "PLANNING_PEER_REVIEW": ["peer_review.md"],
        "PLANNING_REVISION": ["plan.md", "assumptions.md", "risk.md", "todo_breakdown.md"],
    },
    "executor": {
        "EXECUTION": ["implementation_plan.md", "changed_files.md", "patch.diff", "patch_metadata.md", "self_check.md"],
        "PATCH_MERGE": ["merged_patch.diff", "merged_patch_metadata.md", "merge_report.md"],
        "MISC_RESPONSE": ["response.md", "notes.md"],
        "FIXING": ["fix_schedule.md", "fix_patch.diff", "patch_metadata.md", "fix_notes.md", "self_check.md"],
        "REVIEW_FIXING": ["fix_schedule.md", "fix_patch.diff", "patch_metadata.md", "fix_notes.md", "self_check.md"],
    },
    "tester": ["build_report.md", "test_report.md", "bug_report.md"],
    "reviewer": {
        "PLAN_REVIEW": ["review_report.md", "selected_plan.md"],
        "REVIEWING": ["review_report.md"],
    },
    "judge": ["decision.json", "decision_summary.md"],
    "communicator": ["final_delivery.md", "usage_guide.md"],
}


def required_outputs_for(role: str, phase: str) -> list[str]:
    spec = REQUIRED_OUTPUTS[role]
    if isinstance(spec, dict):
        outputs = list(spec[phase])
    else:
        outputs = list(spec)
    if DELIVERY_STATUS_OUTPUT not in outputs:
        outputs.append(DELIVERY_STATUS_OUTPUT)
    return outputs
