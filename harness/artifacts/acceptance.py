from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.artifacts.enums import (
    ORACLE_RESULT_CODE_TO_STATUS,
    ORACLE_RESULT_FAILED,
    ORACLE_RESULT_PASSED,
    REGRESSION_DELTA_NONE,
    TESTER_STATUS_CODE_TO_STATUS,
    VALID_ORACLE_RESULT_CODES,
    VALID_REGRESSION_DELTA_CODES,
    VALID_VERIFICATION_MODE_CODES,
    VERIFICATION_MODE_ABSOLUTE_PASS,
    VERIFICATION_MODE_REGRESSION_DELTA,
    allowed_codes,
    numeric_code,
)


SELECTED_PLAN_ARTIFACT = "selected_plan.json"
ORACLE_RESULTS_FIELD = "oracle_results"
ACCEPTANCE_ORACLES_FIELD = "acceptance_oracles"

VALID_ORACLE_KINDS = {"runtime", "test", "static", "manual"}
VALID_ORACLE_RESULT_STATUSES = {"passed", "failed", "blocked", "not_run"}
VALID_ORACLE_OWNERS = {"tester", "reviewer", "external_evaluator", "harness", "manual"}
VALID_ORACLE_STAGES = {"pre_delivery", "post_delivery", "runtime_readiness", "regression", "manual"}


@dataclass(frozen=True)
class AcceptanceOracle:
    oracle_id: str
    required: bool
    verification_mode_code: int
    payload: dict[str, Any]


def load_selected_plan(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return payload


def acceptance_oracles_from_payload(payload: dict[str, Any]) -> tuple[AcceptanceOracle, ...]:
    oracles = payload.get(ACCEPTANCE_ORACLES_FIELD)
    if not isinstance(oracles, list):
        return ()
    parsed: list[AcceptanceOracle] = []
    for item in oracles:
        if not isinstance(item, dict):
            continue
        oracle_id = _string_field(item, "id")
        required = item.get("required_for_tester")
        if not isinstance(required, bool):
            required = item.get("required")
        verification_mode_code = numeric_code(item, "verification_mode_code") or VERIFICATION_MODE_ABSOLUTE_PASS
        if oracle_id and isinstance(required, bool):
            parsed.append(
                AcceptanceOracle(
                    oracle_id=oracle_id,
                    required=required,
                    verification_mode_code=verification_mode_code,
                    payload=item,
                )
            )
    return tuple(parsed)


def validate_acceptance_oracles(
    payload: dict[str, Any],
    *,
    artifact_name: str = SELECTED_PLAN_ARTIFACT,
    require_numeric_codes: bool = False,
) -> list[str]:
    messages: list[str] = []
    oracles = payload.get(ACCEPTANCE_ORACLES_FIELD)
    if not isinstance(oracles, list) or not oracles:
        return [f"{artifact_name}.{ACCEPTANCE_ORACLES_FIELD} must be a non-empty list"]

    seen_ids: set[str] = set()
    for index, oracle in enumerate(oracles):
        prefix = f"{artifact_name}.{ACCEPTANCE_ORACLES_FIELD}[{index}]"
        if not isinstance(oracle, dict):
            messages.append(f"{prefix} must be an object")
            continue
        oracle_id = _string_field(oracle, "id")
        if not oracle_id:
            messages.append(f"{prefix}.id must be a non-empty string")
        elif oracle_id in seen_ids:
            messages.append(f"{prefix}.id must be unique")
        else:
            seen_ids.add(oracle_id)
        if not _string_field(oracle, "description"):
            messages.append(f"{prefix}.description must be a non-empty string")
        kind = _string_field(oracle, "kind")
        if kind not in VALID_ORACLE_KINDS:
            allowed = ", ".join(sorted(VALID_ORACLE_KINDS))
            messages.append(f"{prefix}.kind must be one of: {allowed}")
        verification_mode_code = numeric_code(oracle, "verification_mode_code")
        if verification_mode_code is None:
            if require_numeric_codes:
                messages.append(
                    f"{prefix}.verification_mode_code must be an integer code: "
                    f"{allowed_codes(VALID_VERIFICATION_MODE_CODES)}"
                )
        elif verification_mode_code not in VALID_VERIFICATION_MODE_CODES:
            messages.append(
                f"{prefix}.verification_mode_code must be one of: "
                f"{allowed_codes(VALID_VERIFICATION_MODE_CODES)}"
            )
        if not isinstance(oracle.get("required"), bool):
            messages.append(f"{prefix}.required must be a boolean")
        owner = _string_field(oracle, "owner")
        if owner not in VALID_ORACLE_OWNERS:
            allowed = ", ".join(sorted(VALID_ORACLE_OWNERS))
            messages.append(f"{prefix}.owner must be one of: {allowed}")
        stage = _string_field(oracle, "stage")
        if stage not in VALID_ORACLE_STAGES:
            allowed = ", ".join(sorted(VALID_ORACLE_STAGES))
            messages.append(f"{prefix}.stage must be one of: {allowed}")
        if not _string_field(oracle, "runtime"):
            messages.append(f"{prefix}.runtime must be a non-empty string")
        for field in ("required_for_tester", "required_for_final"):
            if not isinstance(oracle.get(field), bool):
                messages.append(f"{prefix}.{field} must be a boolean")
        for field in ("commands", "must_contain", "must_not_contain", "semantic_assertions"):
            messages.extend(_validate_string_list(oracle, field, prefix))
        for field in ("expected_exception", "failure_signal", "evidence_hint"):
            value = oracle.get(field, "")
            if value is not None and not isinstance(value, str):
                messages.append(f"{prefix}.{field} must be a string")
        if not _oracle_has_signal(oracle):
            messages.append(
                f"{prefix} must define at least one verification signal: commands, "
                "must_contain, must_not_contain, expected_exception, semantic_assertions, or failure_signal"
            )
    return messages


def validate_tester_oracle_results(
    payload: dict[str, Any],
    selected_oracles: tuple[AcceptanceOracle, ...] | list[AcceptanceOracle] | None = None,
    *,
    artifact_name: str = "tester_result.json",
) -> list[str]:
    messages = validate_tester_oracle_result_shape(payload, artifact_name=artifact_name)
    results = payload.get(ORACLE_RESULTS_FIELD)
    if not isinstance(results, list):
        return messages

    if selected_oracles is None:
        return messages

    selected = tuple(selected_oracles)
    expected_by_id = {oracle.oracle_id: oracle for oracle in selected}
    required_ids = {oracle.oracle_id for oracle in selected if oracle.required}
    seen_ids: set[str] = set()
    statuses_by_id: dict[str, str] = {}
    results_by_id: dict[str, dict[str, Any]] = {}
    prefixes_by_id: dict[str, str] = {}

    for index, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        prefix = f"{artifact_name}.{ORACLE_RESULTS_FIELD}[{index}]"
        oracle_id = _string_field(result, "oracle_id")
        if not oracle_id:
            continue
        if oracle_id in seen_ids:
            continue
        seen_ids.add(oracle_id)
        results_by_id[oracle_id] = result
        prefixes_by_id[oracle_id] = prefix
        if expected_by_id and oracle_id not in expected_by_id:
            messages.append(f"{prefix}.oracle_id {oracle_id!r} is not defined in selected_plan.json")
            continue
        status = _oracle_result_status(result)
        if status in VALID_ORACLE_RESULT_STATUSES:
            statuses_by_id[oracle_id] = status

    missing_required_ids = sorted(required_ids - seen_ids)
    if missing_required_ids:
        messages.append(
            f"{artifact_name}.{ORACLE_RESULTS_FIELD} missing required oracle result(s): "
            + ", ".join(missing_required_ids)
        )

    for oracle_id, result in results_by_id.items():
        oracle = expected_by_id.get(oracle_id)
        if oracle is None or oracle.verification_mode_code != VERIFICATION_MODE_REGRESSION_DELTA:
            continue
        if statuses_by_id.get(oracle_id) == "passed":
            messages.extend(_validate_regression_delta_pass(result, prefixes_by_id[oracle_id]))

    tester_status = _tester_status(payload)
    environment_dependency_issue = payload.get("environment_dependency_issue")
    if tester_status == "tests_passed":
        failed_ids = sorted(
            oracle_id
            for oracle_id, status in statuses_by_id.items()
            if oracle_id in required_ids and status != "passed"
        )
        if failed_ids:
            messages.append(
                f"{artifact_name}.status cannot be tests_passed while required-for-tester oracle(s) "
                "are not passed: "
                + ", ".join(failed_ids)
            )
    if tester_status == "source_bug" and environment_dependency_issue is not True:
        failed_required_ids = sorted(
            oracle_id
            for oracle_id, status in statuses_by_id.items()
            if oracle_id in required_ids and status == "failed"
        )
        if not failed_required_ids:
            messages.append(
                f"{artifact_name}.status source_bug requires at least one failed required-for-tester oracle result"
            )
    return messages


def validate_tester_oracle_result_shape(
    payload: dict[str, Any],
    *,
    artifact_name: str = "tester_result.json",
    require_numeric_codes: bool = False,
) -> list[str]:
    """Validate tester oracle result shape without selected_plan-dependent routing semantics."""
    messages: list[str] = []
    results = payload.get(ORACLE_RESULTS_FIELD)
    if not isinstance(results, list):
        return [f"{artifact_name}.{ORACLE_RESULTS_FIELD} must be a list"]

    seen_ids: set[str] = set()
    for index, result in enumerate(results):
        prefix = f"{artifact_name}.{ORACLE_RESULTS_FIELD}[{index}]"
        if not isinstance(result, dict):
            messages.append(f"{prefix} must be an object")
            continue
        oracle_id = _string_field(result, "oracle_id")
        if not oracle_id:
            messages.append(f"{prefix}.oracle_id must be a non-empty string")
        elif oracle_id in seen_ids:
            messages.append(f"{prefix}.oracle_id must be unique")
        else:
            seen_ids.add(oracle_id)
        result_code = numeric_code(result, "oracle_result_code")
        status = _string_field(result, "status")
        if result_code is None:
            if require_numeric_codes:
                messages.append(
                    f"{prefix}.oracle_result_code must be an integer code: "
                    f"{allowed_codes(VALID_ORACLE_RESULT_CODES)}"
                )
            elif status not in VALID_ORACLE_RESULT_STATUSES:
                allowed = ", ".join(sorted(VALID_ORACLE_RESULT_STATUSES))
                messages.append(f"{prefix}.status must be one of: {allowed}")
        elif result_code not in VALID_ORACLE_RESULT_CODES:
            messages.append(
                f"{prefix}.oracle_result_code must be one of: {allowed_codes(VALID_ORACLE_RESULT_CODES)}"
            )
        elif status and status != ORACLE_RESULT_CODE_TO_STATUS[result_code]:
            messages.append(
                f"{prefix}.status {status!r} does not match oracle_result_code {result_code}"
            )
        for code_field in ("baseline_result_code", "current_result_code"):
            code = numeric_code(result, code_field)
            if code is not None and code not in VALID_ORACLE_RESULT_CODES:
                messages.append(f"{prefix}.{code_field} must be one of: {allowed_codes(VALID_ORACLE_RESULT_CODES)}")
        delta_code = numeric_code(result, "regression_delta_code")
        if delta_code is not None and delta_code not in VALID_REGRESSION_DELTA_CODES:
            messages.append(
                f"{prefix}.regression_delta_code must be one of: {allowed_codes(VALID_REGRESSION_DELTA_CODES)}"
            )
        if not _string_field(result, "evidence"):
            messages.append(f"{prefix}.evidence must be a non-empty string")
        if "commands_run" not in result or not isinstance(result.get("commands_run"), list):
            messages.append(f"{prefix}.commands_run must be a list")
        output_excerpt = result.get("output_excerpt", "")
        if output_excerpt is not None and not isinstance(output_excerpt, str):
            messages.append(f"{prefix}.output_excerpt must be a string")
    return messages


def _tester_status(payload: dict[str, Any]) -> str:
    status_code = numeric_code(payload, "tester_status_code")
    if status_code in TESTER_STATUS_CODE_TO_STATUS:
        return TESTER_STATUS_CODE_TO_STATUS[status_code]
    return _string_field(payload, "status")


def _oracle_result_status(result: dict[str, Any]) -> str:
    result_code = numeric_code(result, "oracle_result_code")
    if result_code in ORACLE_RESULT_CODE_TO_STATUS:
        return ORACLE_RESULT_CODE_TO_STATUS[result_code]
    return _string_field(result, "status")


def _validate_regression_delta_pass(result: dict[str, Any], prefix: str) -> list[str]:
    baseline_code = numeric_code(result, "baseline_result_code")
    current_code = numeric_code(result, "current_result_code")
    delta_code = numeric_code(result, "regression_delta_code")
    runnable_codes = {ORACLE_RESULT_PASSED, ORACLE_RESULT_FAILED}
    if baseline_code in runnable_codes and current_code in runnable_codes and delta_code == REGRESSION_DELTA_NONE:
        return []
    return [
        f"{prefix} regression_delta pass requires baseline_result_code/current_result_code "
        f"0 or 1 and regression_delta_code {REGRESSION_DELTA_NONE}"
    ]


def _oracle_has_signal(oracle: dict[str, Any]) -> bool:
    for field in ("commands", "must_contain", "must_not_contain", "semantic_assertions"):
        value = oracle.get(field)
        if isinstance(value, list) and any(isinstance(item, str) and item.strip() for item in value):
            return True
    return any(_string_field(oracle, field) for field in ("expected_exception", "failure_signal"))


def _validate_string_list(payload: dict[str, Any], field: str, prefix: str) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list):
        return [f"{prefix}.{field} must be a list"]
    if not all(isinstance(item, str) for item in value):
        return [f"{prefix}.{field} must contain only strings"]
    return []


def _string_field(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    return value.strip() if isinstance(value, str) else ""
