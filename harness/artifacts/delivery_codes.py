from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryReturnCode:
    code: int
    status: str
    label: str
    description: str


DELIVERY_SUCCESS_RETURN_CODE = 0

# Canonical role delivery return-code table.
# Keep this table as the single source of truth for validator behavior and
# Agent-facing prompt text. These codes describe whether a role satisfied its
# output contract; they do not describe business verdicts such as test pass/fail.
DELIVERY_RETURN_CODES: tuple[DeliveryReturnCode, ...] = (
    DeliveryReturnCode(0, "success", "complete", "role delivery files are complete and Harness may accept the role run"),
    DeliveryReturnCode(1, "partial", "partial", "partial role delivery; useful files may exist but the contract is incomplete"),
    DeliveryReturnCode(2, "partial", "blocked", "blocked by missing input, context, or evidence required to complete the role"),
    DeliveryReturnCode(3, "partial", "degraded", "degraded role delivery that requires manual review before it can be trusted"),
    DeliveryReturnCode(-1, "failed", "unusable", "role failed to produce a usable result"),
    DeliveryReturnCode(-2, "failed", "invalid_outputs", "required role outputs are missing, empty, or invalid"),
    DeliveryReturnCode(-3, "failed", "runtime_error", "tool, runtime, adapter, or internal execution error"),
)

DELIVERY_RETURN_CODE_BY_CODE = {entry.code: entry for entry in DELIVERY_RETURN_CODES}


def delivery_status_for_return_code(return_code: int) -> str:
    entry = DELIVERY_RETURN_CODE_BY_CODE.get(return_code)
    if entry is not None:
        return entry.status
    return "failed" if return_code < 0 else "partial"


def delivery_return_code_meanings_text() -> str:
    return "; ".join(f"`{entry.code}` = {entry.description}" for entry in DELIVERY_RETURN_CODES)


def markdown_artifact_code_meanings_text() -> str:
    return "; ".join(
        [
            "`0` = this Markdown artifact is complete and usable",
            "`1` = this Markdown artifact is partial",
            "`2` = this Markdown artifact is blocked by missing input, context, or evidence",
            "`3` = this Markdown artifact is degraded and needs manual review",
            "`-1` = this Markdown artifact does not contain a usable result",
            "`-2` = this Markdown artifact is missing required sections or has invalid structure",
            "`-3` = this Markdown artifact could not be produced because of a tool, runtime, adapter, or internal error",
        ]
    )


def markdown_business_code_contract_lines() -> list[str]:
    return [
        "- Every required Markdown deliverable except `delivery.md` must contain `artifact_result_code: <integer>`.",
        f"- Markdown artifact result code meanings: {markdown_artifact_code_meanings_text()}.",
        "- Use `artifact_result_code: 0` when that Markdown file is complete, even if the business verdict described inside it is negative.",
        "- Any business verdict in a Markdown deliverable must use a numeric `*_code` field, not a natural-language enum field.",
        "- Required Markdown verdict fields: `test_result_code`, `build_result_code`, `bug_result_code`, `review_decision_code`, `peer_review_code`, `decision_code`, `final_delivery_code`, or another task-specific `*_code` field when applicable.",
        "- Standard Markdown business verdict code meanings: `0` accepted/pass/approved/satisfied/no blocking issue; `1` changes requested or non-blocking issue; `2` blocked/not testable/missing evidence; `3` manual review required; `-1` failed/rejected/blocking issue; `-2` invalid or missing evidence; `-3` tool/runtime/internal error.",
        "- Do not write Markdown verdict fields such as `status: success`, `test_result: pass`, `review_decision: approved`, or `peer_review_status: satisfied`; use numeric `*_code` fields instead.",
    ]


def delivery_return_code_contract_lines() -> list[str]:
    return [
        "- `delivery.md` is the JSON role return envelope, not the task/business verdict.",
        "- `delivery.md` must be exactly one JSON object with no Markdown, prose, code fence, YAML, table, or bullet text.",
        '- Required JSON shape: `{"return_code":0,"task_status":"success","role_return_code":0,"produced_files":["delivery.md"],"known_risks":[]}`.',
        "- `return_code` must be a JSON integer.",
        f"- Return code meanings: {delivery_return_code_meanings_text()}.",
        "- Use JSON `return_code: 0` only when this role successfully returned all required output files for this phase.",
        "- Use a non-zero return code when the role output contract is not cleanly satisfied.",
        "- Do not use natural-language delivery status text such as `status: success`, `status: failed`, `pass`, `fail`, `approved`, `rejected`, `ok`, or `complete` as a replacement for JSON `return_code`.",
        "- Business outcomes must use separate machine fields in the phase artifacts, never the `delivery.md` return code. Examples: `decision.json.decision: fail`, `test_result_code: -1`, `review_decision_code: 1`, `peer_review_code: 1`.",
        "- If you rendered a clear negative business verdict, such as tests failed or changes are required, still write JSON `return_code: 0` in `delivery.md` when the required role files are complete.",
        "- Harness validates `delivery.md`; any return code other than `0` prevents the run from advancing.",
    ]
