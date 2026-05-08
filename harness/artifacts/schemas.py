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


def output_contract_lines_for(role: str, phase: str, required_outputs: list[str]) -> list[str]:
    markdown_outputs = [
        name
        for name in required_outputs
        if name != DELIVERY_STATUS_OUTPUT and name.endswith(".md")
    ]
    markdown_list = ", ".join(f"`{name}`" for name in markdown_outputs)
    base = [
        "- Every role and every phase must create `delivery.md`.",
        "- `delivery.md` is the role return envelope, not the task/business verdict.",
        "- `delivery.md` must contain `return_code: 0` when the required role files are complete.",
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
            "- Put build outcome only in `build_report.md` as `build_result_code: 0`, `build_result_code: -1`, or `build_result_code: 2`.",
            "- Put test outcome only in `test_report.md` as `test_result_code: 0`, `test_result_code: -1`, or `test_result_code: 2`.",
            "- Put bug outcome only in `bug_report.md` as `bug_result_code: 0`, `bug_result_code: 1`, or `bug_result_code: -1`.",
            "- If testing is blocked by a broken implementation, still write complete reports with `artifact_result_code: 0` and describe the blocker in the verdict fields.",
            "- Harness validates `delivery.md` and report headers; any non-zero `return_code` or non-zero `artifact_result_code` prevents the run from advancing.",
        ]
    if role == "reviewer":
        return [
            *base,
            "- Put review outcome only in `review_report.md` as `review_decision_code: 0`, `review_decision_code: 1`, or `review_decision_code: -1`.",
            "- Do not copy `review_decision_code` into `artifact_result_code` or `return_code`.",
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
