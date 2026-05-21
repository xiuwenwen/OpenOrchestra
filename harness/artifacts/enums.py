from __future__ import annotations

from typing import Any


# Model-facing numeric contract codes. Keep these stable; prompts and templates
# reference the numbers, while Harness maps them to existing internal labels.
TESTER_STATUS_TESTS_PASSED = 0
TESTER_STATUS_SOURCE_BUG = 1
TESTER_STATUS_ENVIRONMENT_BLOCKED = 2

NEXT_ACTION_CONTINUE = 0
NEXT_ACTION_FIX_CODE = 1
NEXT_ACTION_BLOCK_TASK = 2

ORACLE_RESULT_PASSED = 0
ORACLE_RESULT_FAILED = 1
ORACLE_RESULT_BLOCKED = 2
ORACLE_RESULT_NOT_RUN = 3

REGRESSION_DELTA_NONE = 0
REGRESSION_DELTA_NEW_FAILURE = 1
REGRESSION_DELTA_UNKNOWN = 2

VERIFICATION_MODE_ABSOLUTE_PASS = 1
VERIFICATION_MODE_REGRESSION_DELTA = 2

TESTER_STATUS_CODE_TO_STATUS = {
    TESTER_STATUS_TESTS_PASSED: "tests_passed",
    TESTER_STATUS_SOURCE_BUG: "source_bug",
    TESTER_STATUS_ENVIRONMENT_BLOCKED: "environment_blocked",
}
TESTER_STATUS_TO_CODE = {value: key for key, value in TESTER_STATUS_CODE_TO_STATUS.items()}

NEXT_ACTION_CODE_TO_ACTION = {
    NEXT_ACTION_CONTINUE: "continue",
    NEXT_ACTION_FIX_CODE: "fix_code",
    NEXT_ACTION_BLOCK_TASK: "block_task",
}
NEXT_ACTION_TO_CODE = {value: key for key, value in NEXT_ACTION_CODE_TO_ACTION.items()}

ORACLE_RESULT_CODE_TO_STATUS = {
    ORACLE_RESULT_PASSED: "passed",
    ORACLE_RESULT_FAILED: "failed",
    ORACLE_RESULT_BLOCKED: "blocked",
    ORACLE_RESULT_NOT_RUN: "not_run",
}
ORACLE_RESULT_STATUS_TO_CODE = {value: key for key, value in ORACLE_RESULT_CODE_TO_STATUS.items()}

VALID_TESTER_STATUS_CODES = set(TESTER_STATUS_CODE_TO_STATUS)
VALID_NEXT_ACTION_CODES = set(NEXT_ACTION_CODE_TO_ACTION)
VALID_ORACLE_RESULT_CODES = set(ORACLE_RESULT_CODE_TO_STATUS)
VALID_REGRESSION_DELTA_CODES = {
    REGRESSION_DELTA_NONE,
    REGRESSION_DELTA_NEW_FAILURE,
    REGRESSION_DELTA_UNKNOWN,
}
VALID_VERIFICATION_MODE_CODES = {
    VERIFICATION_MODE_ABSOLUTE_PASS,
    VERIFICATION_MODE_REGRESSION_DELTA,
}


def numeric_code(payload: dict[str, Any], field: str) -> int | None:
    value = payload.get(field)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def allowed_codes(codes: set[int]) -> str:
    return ", ".join(str(code) for code in sorted(codes))
