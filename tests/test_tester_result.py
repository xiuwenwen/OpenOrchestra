from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.testing.tester_result import TesterResultError as HarnessTesterResultError
from harness.testing.tester_result import load_tester_result


def _write_tester_result(
    path: Path,
    *,
    status: str = "tests_passed",
    next_action: str = "continue",
    environment_dependency_issue: bool | None = False,
) -> None:
    payload = {
        "schema_version": 1,
        "status": status,
        "next_action": next_action,
        "failure_type": "none" if status == "tests_passed" else status,
        "summary": status,
        "setup_commands_run": [],
        "test_commands_run": [],
        "oracle_results": [
            {
                "oracle_id": "A1",
                "status": "passed" if status == "tests_passed" else "failed",
                "evidence": "oracle evidence",
                "commands_run": ["pytest"],
                "output_excerpt": "",
            }
        ],
        "remaining_blockers": [],
    }
    if environment_dependency_issue is not None:
        payload["environment_dependency_issue"] = environment_dependency_issue
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_tester_result_requires_environment_dependency_issue_field(tmp_path: Path) -> None:
    path = tmp_path / "tester_result.json"
    _write_tester_result(path, environment_dependency_issue=None)

    with pytest.raises(HarnessTesterResultError, match="environment_dependency_issue"):
        load_tester_result(path)


def test_tester_result_preserves_environment_dependency_issue_before_status(tmp_path: Path) -> None:
    path = tmp_path / "tester_result.json"
    _write_tester_result(
        path,
        status="source_bug",
        next_action="fix_code",
        environment_dependency_issue=True,
    )

    result = load_tester_result(path)

    assert result.source_bug
    assert result.has_environment_dependency_issue


def test_environment_blocked_requires_environment_dependency_issue(tmp_path: Path) -> None:
    path = tmp_path / "tester_result.json"
    _write_tester_result(
        path,
        status="environment_blocked",
        next_action="block_task",
        environment_dependency_issue=False,
    )

    with pytest.raises(HarnessTesterResultError, match="environment_dependency_issue=true"):
        load_tester_result(path)


def test_tester_result_requires_oracle_results_field(tmp_path: Path) -> None:
    path = tmp_path / "tester_result.json"
    _write_tester_result(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("oracle_results")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HarnessTesterResultError, match="oracle_results"):
        load_tester_result(path)


def test_tests_passed_rejects_failed_oracle_result(tmp_path: Path) -> None:
    path = tmp_path / "tester_result.json"
    _write_tester_result(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["oracle_results"][0]["status"] = "failed"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HarnessTesterResultError, match="tests_passed"):
        load_tester_result(path)
