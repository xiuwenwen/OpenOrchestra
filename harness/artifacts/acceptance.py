from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SELECTED_PLAN_ARTIFACT = "selected_plan.json"
ORACLE_RESULTS_FIELD = "oracle_results"
ACCEPTANCE_ORACLES_FIELD = "acceptance_oracles"

VALID_ORACLE_KINDS = {"runtime", "test", "static", "manual"}
VALID_ORACLE_RESULT_STATUSES = {"passed", "failed", "blocked", "not_run"}


@dataclass(frozen=True)
class AcceptanceOracle:
    oracle_id: str
    required: bool
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
        required = item.get("required")
        if oracle_id and isinstance(required, bool):
            parsed.append(AcceptanceOracle(oracle_id=oracle_id, required=required, payload=item))
    return tuple(parsed)


def validate_acceptance_oracles(
    payload: dict[str, Any],
    *,
    artifact_name: str = SELECTED_PLAN_ARTIFACT,
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
        if not isinstance(oracle.get("required"), bool):
            messages.append(f"{prefix}.required must be a boolean")
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
    messages: list[str] = []
    results = payload.get(ORACLE_RESULTS_FIELD)
    if not isinstance(results, list):
        return [f"{artifact_name}.{ORACLE_RESULTS_FIELD} must be a list"]

    selected = tuple(selected_oracles or ())
    expected_by_id = {oracle.oracle_id: oracle for oracle in selected}
    required_ids = {oracle.oracle_id for oracle in selected if oracle.required}
    seen_ids: set[str] = set()
    statuses_by_id: dict[str, str] = {}

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
            if expected_by_id and oracle_id not in expected_by_id:
                messages.append(f"{prefix}.oracle_id {oracle_id!r} is not defined in selected_plan.json")
        status = _string_field(result, "status")
        if status not in VALID_ORACLE_RESULT_STATUSES:
            allowed = ", ".join(sorted(VALID_ORACLE_RESULT_STATUSES))
            messages.append(f"{prefix}.status must be one of: {allowed}")
        elif oracle_id:
            statuses_by_id[oracle_id] = status
        if not _string_field(result, "evidence"):
            messages.append(f"{prefix}.evidence must be a non-empty string")
        if "commands_run" not in result or not isinstance(result.get("commands_run"), list):
            messages.append(f"{prefix}.commands_run must be a list")
        output_excerpt = result.get("output_excerpt", "")
        if output_excerpt is not None and not isinstance(output_excerpt, str):
            messages.append(f"{prefix}.output_excerpt must be a string")

    missing_required_ids = sorted(required_ids - seen_ids)
    if missing_required_ids:
        messages.append(
            f"{artifact_name}.{ORACLE_RESULTS_FIELD} missing required oracle result(s): "
            + ", ".join(missing_required_ids)
        )

    tester_status = _string_field(payload, "status")
    environment_dependency_issue = payload.get("environment_dependency_issue")
    if tester_status == "tests_passed":
        failed_ids = sorted(
            oracle_id
            for oracle_id, status in statuses_by_id.items()
            if (not expected_by_id or oracle_id in required_ids) and status != "passed"
        )
        if failed_ids:
            messages.append(
                f"{artifact_name}.status cannot be tests_passed while required oracle(s) are not passed: "
                + ", ".join(failed_ids)
            )
    if tester_status == "source_bug" and environment_dependency_issue is not True:
        failed_required_ids = sorted(
            oracle_id
            for oracle_id, status in statuses_by_id.items()
            if status == "failed" and (not expected_by_id or oracle_id in required_ids)
        )
        if selected and not failed_required_ids:
            messages.append(
                f"{artifact_name}.status source_bug requires at least one failed required oracle result"
            )
    return messages


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
