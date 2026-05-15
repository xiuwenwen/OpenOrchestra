from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PEER_REVIEW_RESULT_ARTIFACT = "peer_review_result.json"
PEER_REVIEW_CODES = {-1, 0, 1}


def parse_peer_review_result_content(content: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def load_peer_review_result(path: Path) -> dict[str, Any]:
    payload = parse_peer_review_result_content(path.read_text(encoding="utf-8", errors="replace"))
    return payload or {}


def peer_review_code_from_payload(payload: dict[str, Any]) -> int | None:
    value = payload.get("peer_review_code")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def validate_peer_review_result_payload(payload: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    code = peer_review_code_from_payload(payload)
    if code not in PEER_REVIEW_CODES:
        messages.append("peer_review_result.json.peer_review_code must be one of -1, 0, or 1")
    status = str(payload.get("peer_review_status") or payload.get("status") or "").strip().lower()
    if not status:
        messages.append("peer_review_result.json.peer_review_status is required")
    elif status not in {"satisfied", "changes_requested", "blocked"}:
        messages.append(
            'peer_review_result.json.peer_review_status must be "satisfied", "changes_requested", or "blocked"'
        )
    if code == 0 and status and status != "satisfied":
        messages.append("peer_review_code 0 requires peer_review_status satisfied")
    if code in {1, -1} and status == "satisfied":
        messages.append("non-zero peer_review_code cannot use peer_review_status satisfied")
    if not isinstance(payload.get("summary", ""), str):
        messages.append("peer_review_result.json.summary must be a string")
    required_changes = payload.get("required_changes", [])
    if required_changes is not None and not isinstance(required_changes, list):
        messages.append("peer_review_result.json.required_changes must be a list")
    return messages
