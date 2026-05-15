from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.artifacts.acceptance import ORACLE_RESULTS_FIELD, validate_tester_oracle_results


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

    status = _string_field(payload, "status").lower()
    if status not in VALID_TESTER_STATUSES:
        allowed = ", ".join(sorted(VALID_TESTER_STATUSES))
        raise TesterResultError(f"{path} has invalid status {status!r}; expected one of: {allowed}")

    expected_action = NEXT_ACTION_BY_STATUS[status]
    next_action = _string_field(payload, "next_action") or expected_action
    if next_action != expected_action:
        raise TesterResultError(
            f"{path} has next_action {next_action!r}, but status {status!r} requires {expected_action!r}"
        )

    environment_dependency_issue = _required_bool_field(payload, "environment_dependency_issue", path)
    if status == TESTS_PASSED and environment_dependency_issue:
        raise TesterResultError(f"{path} cannot report tests_passed while environment_dependency_issue is true")
    if status == ENVIRONMENT_BLOCKED and not environment_dependency_issue:
        raise TesterResultError(f"{path} must set environment_dependency_issue=true for environment_blocked")

    failure_type = _string_field(payload, "failure_type") or DEFAULT_FAILURE_TYPE_BY_STATUS[status]
    summary = _string_field(payload, "summary")
    oracle_errors = validate_tester_oracle_results(payload)
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
