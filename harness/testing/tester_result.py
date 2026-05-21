from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.artifacts.enums import (
    NEXT_ACTION_CODE_TO_ACTION,
    NEXT_ACTION_TO_CODE,
    TESTER_STATUS_CODE_TO_STATUS,
    TESTER_STATUS_TO_CODE,
    VALID_NEXT_ACTION_CODES,
    VALID_TESTER_STATUS_CODES,
    allowed_codes,
    numeric_code,
)
from harness.artifacts.acceptance import ORACLE_RESULTS_FIELD, validate_tester_oracle_result_shape
from harness.core.taxonomy import FAILURE_TYPES, RUNTIME_BLOCKER_FAILURE_TYPES


TESTER_RESULT_ARTIFACT = "tester_result.json"
TESTS_PASSED = "tests_passed"
SOURCE_BUG = "source_bug"
ENVIRONMENT_BLOCKED = "environment_blocked"
VALID_TESTER_STATUSES = {TESTS_PASSED, SOURCE_BUG, ENVIRONMENT_BLOCKED}
NEXT_ACTION_BY_STATUS = {
    TESTS_PASSED: "continue",
    SOURCE_BUG: "fix_code",
    ENVIRONMENT_BLOCKED: "block_task",
}
DEFAULT_FAILURE_TYPE_BY_STATUS = {
    TESTS_PASSED: "none",
    SOURCE_BUG: "source_bug",
    ENVIRONMENT_BLOCKED: "env_setup",
}


class TesterResultError(ValueError):
    pass


@dataclass(frozen=True)
class TesterResult:
    status: str
    next_action: str
    failure_type: str
    summary: str
    artifact_path: Path
    payload: dict[str, Any]
    environment_dependency_issue: bool = False
    oracle_results: tuple[dict[str, Any], ...] = ()
    environment_ready: bool | None = None

    @property
    def tests_passed(self) -> bool:
        return self.status == TESTS_PASSED

    @property
    def source_bug(self) -> bool:
        return self.status == SOURCE_BUG

    @property
    def environment_blocked(self) -> bool:
        return self.status == ENVIRONMENT_BLOCKED

    @property
    def has_environment_dependency_issue(self) -> bool:
        return self.environment_dependency_issue


def load_tester_result(path: Path) -> TesterResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TesterResultError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TesterResultError(f"{path} must contain one JSON object")

    status_code = numeric_code(payload, "tester_status_code")
    status_text = _string_field(payload, "status").lower()
    if status_code is not None:
        if status_code not in VALID_TESTER_STATUS_CODES:
            raise TesterResultError(
                f"{path} has invalid tester_status_code {status_code!r}; expected one of: "
                f"{allowed_codes(VALID_TESTER_STATUS_CODES)}"
            )
        status = TESTER_STATUS_CODE_TO_STATUS[status_code]
        if status_text and status_text != status:
            raise TesterResultError(
                f"{path} status {status_text!r} does not match tester_status_code {status_code}"
            )
    else:
        status = status_text
    if status not in VALID_TESTER_STATUSES:
        allowed = ", ".join(sorted(VALID_TESTER_STATUSES))
        raise TesterResultError(f"{path} has invalid status {status!r}; expected one of: {allowed}")

    expected_action = NEXT_ACTION_BY_STATUS[status]
    next_action_code = numeric_code(payload, "next_action_code")
    next_action_text = _string_field(payload, "next_action")
    if next_action_code is not None:
        if next_action_code not in VALID_NEXT_ACTION_CODES:
            raise TesterResultError(
                f"{path} has invalid next_action_code {next_action_code!r}; expected one of: "
                f"{allowed_codes(VALID_NEXT_ACTION_CODES)}"
            )
        next_action = NEXT_ACTION_CODE_TO_ACTION[next_action_code]
        if next_action_text and next_action_text != next_action:
            raise TesterResultError(
                f"{path} next_action {next_action_text!r} does not match next_action_code {next_action_code}"
            )
    else:
        next_action = next_action_text or expected_action
    if next_action != expected_action:
        raise TesterResultError(
            f"{path} has next_action {next_action!r}, but status {status!r} requires {expected_action!r}"
        )
    expected_action_code = NEXT_ACTION_TO_CODE[expected_action]
    if next_action_code is not None and next_action_code != expected_action_code:
        raise TesterResultError(
            f"{path} has next_action_code {next_action_code!r}, but tester_status_code "
            f"{TESTER_STATUS_TO_CODE[status]!r} requires {expected_action_code!r}"
        )

    environment_dependency_issue = _required_bool_field(payload, "environment_dependency_issue", path)
    environment_ready = _optional_bool_field(payload, "environment_ready", path)
    if status == TESTS_PASSED and environment_dependency_issue:
        raise TesterResultError(f"{path} cannot report tests_passed while environment_dependency_issue is true")
    if status == ENVIRONMENT_BLOCKED and not environment_dependency_issue:
        raise TesterResultError(f"{path} must set environment_dependency_issue=true for environment_blocked")

    failure_type = _string_field(payload, "failure_type") or DEFAULT_FAILURE_TYPE_BY_STATUS[status]
    if failure_type not in FAILURE_TYPES:
        allowed = ", ".join(sorted(FAILURE_TYPES))
        raise TesterResultError(f"{path} has invalid failure_type {failure_type!r}; expected one of: {allowed}")
    if failure_type in RUNTIME_BLOCKER_FAILURE_TYPES and not environment_dependency_issue:
        raise TesterResultError(
            f"{path} must set environment_dependency_issue=true for runtime, environment, command, infra, or contract failure_type"
        )
    summary = _string_field(payload, "summary")
    oracle_errors = validate_tester_oracle_result_shape(payload)
    if oracle_errors:
        raise TesterResultError(f"{path} has invalid oracle_results: {'; '.join(oracle_errors)}")
    oracle_results = payload.get(ORACLE_RESULTS_FIELD)
    return TesterResult(
        status=status,
        next_action=next_action,
        failure_type=failure_type,
        summary=summary,
        artifact_path=path,
        payload=payload,
        environment_ready=environment_ready,
        environment_dependency_issue=environment_dependency_issue,
        oracle_results=tuple(item for item in oracle_results if isinstance(item, dict)),
    )


def _string_field(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _required_bool_field(payload: dict[str, Any], field: str, path: Path) -> bool:
    if field not in payload:
        raise TesterResultError(f"{path} is missing required boolean field {field!r}")
    value = payload[field]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise TesterResultError(f"{path} field {field!r} must be a boolean")


def _optional_bool_field(payload: dict[str, Any], field: str, path: Path) -> bool | None:
    if field not in payload:
        return None
    value = payload[field]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise TesterResultError(f"{path} field {field!r} must be a boolean when present")
