from __future__ import annotations

import pytest

from harness.judge.decision_parser import DecisionParseError, parse_decision_text


def test_decision_parser_accepts_strict_json() -> None:
    payload = parse_decision_text('{"decision":"approved","changes_required":false}')

    assert payload == {"decision": "approved", "changes_required": False}


def test_decision_parser_accepts_unescaped_newline_inside_string() -> None:
    payload = parse_decision_text('{"decision":"approved","rationale":"line one\nline two"}')

    assert payload["decision"] == "approved"
    assert payload["rationale"] == "line one\nline two"


def test_decision_parser_extracts_fenced_json() -> None:
    payload = parse_decision_text('Decision:\n```json\n{"decision":"pass","tests_passed":true}\n```')

    assert payload["decision"] == "pass"
    assert payload["tests_passed"] is True


def test_decision_parser_recovers_top_level_decision_from_malformed_json() -> None:
    payload = parse_decision_text('{\n  "decision": "approved\',\n  "rationale": "bad quote"\n}')

    assert payload["decision"] == "approved"
    assert payload["parse_warning"]


def test_decision_parser_fails_without_recoverable_decision() -> None:
    with pytest.raises(DecisionParseError):
        parse_decision_text("not a decision")
