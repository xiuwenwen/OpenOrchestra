from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class DecisionParseError(ValueError):
    pass


def parse_decision_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return parse_decision_text(text)
    except DecisionParseError as exc:
        raise DecisionParseError(f"Could not parse judge decision file {path}: {exc}") from exc


def parse_decision_text(text: str) -> dict[str, Any]:
    candidates = _json_candidates(text)
    errors: list[str] = []
    for candidate in candidates:
        for normalized in _normalized_json_candidates(candidate):
            try:
                payload = json.loads(normalized)
            except json.JSONDecodeError as exc:
                errors.append(str(exc))
                try:
                    payload = json.loads(normalized, strict=False)
                except json.JSONDecodeError as strict_exc:
                    errors.append(str(strict_exc))
                    continue
            if isinstance(payload, dict):
                return payload
            errors.append("JSON payload is not an object")

    fallback = _extract_decision_fields(text)
    if fallback:
        return fallback
    detail = errors[-1] if errors else "no JSON object found"
    raise DecisionParseError(detail)


def _json_candidates(text: str) -> list[str]:
    stripped = text.strip()
    candidates: list[str] = []
    if stripped:
        candidates.append(stripped)
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE):
        candidates.append(match.group(1).strip())
    object_match = re.search(r"\{.*\}", text, re.DOTALL)
    if object_match:
        candidates.append(object_match.group(0).strip())
    return list(dict.fromkeys(candidates))


def _normalized_json_candidates(text: str) -> list[str]:
    normalized = text.replace("\ufeff", "").strip()
    candidates = [normalized]
    repaired = _escape_control_chars_inside_strings(normalized)
    if repaired != normalized:
        candidates.append(repaired)
    quote_repaired = _repair_mismatched_simple_string_values(repaired)
    if quote_repaired != repaired:
        candidates.append(quote_repaired)
    return list(dict.fromkeys(candidates))


def _escape_control_chars_inside_strings(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                result.append(char)
                escaped = False
                continue
            if char == "\\":
                result.append(char)
                escaped = True
                continue
            if char == '"':
                result.append(char)
                in_string = False
                continue
            if char == "\n":
                result.append("\\n")
                continue
            if char == "\r":
                result.append("\\r")
                continue
            if char == "\t":
                result.append("\\t")
                continue
            if ord(char) < 0x20:
                result.append(f"\\u{ord(char):04x}")
                continue
            result.append(char)
            continue
        result.append(char)
        if char == '"':
            in_string = True
    return "".join(result)


def _repair_mismatched_simple_string_values(text: str) -> str:
    return re.sub(
        r'(:\s*)"([^"\n\r{}[\],:]{1,80})\',(\s*["}\]])',
        lambda match: f'{match.group(1)}"{match.group(2)}",{match.group(3)}',
        text,
    )


def _extract_decision_fields(text: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    decision = _extract_field_value(text, "decision") or _extract_field_value(text, "status") or _extract_field_value(text, "result")
    if decision:
        payload["decision"] = decision
    changes_required = _extract_bool_field(text, "changes_required")
    if changes_required is not None:
        payload["changes_required"] = changes_required
    tests_passed = _extract_bool_field(text, "tests_passed")
    if tests_passed is not None:
        payload["tests_passed"] = tests_passed
    final_approved = _extract_bool_field(text, "final_approved")
    if final_approved is not None:
        payload["final_approved"] = final_approved
    if payload:
        payload["parse_warning"] = "decision.json was recovered from malformed JSON"
    return payload


def _extract_field_value(text: str, field_name: str) -> str | None:
    pattern = re.compile(
        rf'["\']?{re.escape(field_name)}["\']?\s*[:=]\s*["\']?([A-Za-z_ -]+)["\']?',
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).strip().strip("\"'").lower().replace(" ", "_").replace("-", "_")


def _extract_bool_field(text: str, field_name: str) -> bool | None:
    pattern = re.compile(
        rf'["\']?{re.escape(field_name)}["\']?\s*[:=]\s*["\']?(true|false)["\']?',
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).lower() == "true"
