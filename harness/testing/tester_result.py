from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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

    @property
    def tests_passed(self) -> bool:
        return self.status == TESTS_PASSED

    @property
    def source_bug(self) -> bool:
        return self.status == SOURCE_BUG

    @property
    def environment_blocked(self) -> bool:
        return self.status == ENVIRONMENT_BLOCKED


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

    failure_type = _string_field(payload, "failure_type") or DEFAULT_FAILURE_TYPE_BY_STATUS[status]
    summary = _string_field(payload, "summary")
    return TesterResult(
        status=status,
        next_action=next_action,
        failure_type=failure_type,
        summary=summary,
        artifact_path=path,
        payload=payload,
    )


def _string_field(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()
