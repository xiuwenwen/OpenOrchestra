from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REVIEW_RESULT_ARTIFACT = "review_result.json"
VALID_REVIEW_DECISION_CODES = {-1, 0, 1}
VALID_REVIEW_STATUSES = {"approved", "changes_required", "blocked"}
VALID_REVIEW_ENVIRONMENT_STATUSES = {"ready", "changes_required", "blocked", "not_applicable"}


def extract_review_decision_code(content: str) -> int | None:
    payload = parse_review_result_content(content)
    if payload:
        return review_decision_code_from_payload(payload)
    return None


def parse_review_result_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if not text or not text.startswith("{"):
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_review_result(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    return parse_review_result_content(path.read_text(encoding="utf-8", errors="replace"))


def review_decision_code_from_payload(payload: dict[str, Any]) -> int | None:
    return _coerce_review_decision_code(payload.get("review_decision_code"))


def validate_review_result_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    decision_code = review_decision_code_from_payload(payload)
    if decision_code is None:
        errors.append("review_result.json review_decision_code must be one of -1, 0, or 1")

    status = str(payload.get("review_status") or "").strip()
    if status not in VALID_REVIEW_STATUSES:
        errors.append("review_result.json review_status must be approved, changes_required, or blocked")
    elif decision_code == 0 and status != "approved":
        errors.append("review_result.json review_status must be approved when review_decision_code is 0")
    elif decision_code == 1 and status != "changes_required":
        errors.append("review_result.json review_status must be changes_required when review_decision_code is 1")
    elif decision_code == -1 and status != "blocked":
        errors.append("review_result.json review_status must be blocked when review_decision_code is -1")

    environment_check = payload.get("environment_check")
    if not isinstance(environment_check, dict):
        errors.append("review_result.json environment_check must be an object")
        return errors

    if not isinstance(environment_check.get("attempted"), bool):
        errors.append("review_result.json environment_check.attempted must be boolean")
    environment_status = str(environment_check.get("status") or "").strip()
    if environment_status not in VALID_REVIEW_ENVIRONMENT_STATUSES:
        errors.append("review_result.json environment_check.status must be ready, changes_required, blocked, or not_applicable")
    if not isinstance(environment_check.get("commands_run"), list):
        errors.append("review_result.json environment_check.commands_run must be a list")
    elif not all(isinstance(command, str) for command in environment_check["commands_run"]):
        errors.append("review_result.json environment_check.commands_run entries must be strings")
    if not isinstance(environment_check.get("fixable"), bool):
        errors.append("review_result.json environment_check.fixable must be boolean")
    if not isinstance(environment_check.get("blocking_reason"), str):
        errors.append("review_result.json environment_check.blocking_reason must be a string")
    if environment_status == "blocked" and status != "blocked":
        errors.append("review_result.json review_status must be blocked when environment_check.status is blocked")
    return errors


def _coerce_review_decision_code(value: Any) -> int | None:
    try:
        code = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return code if code in VALID_REVIEW_DECISION_CODES else None
