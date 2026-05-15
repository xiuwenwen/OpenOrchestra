from __future__ import annotations

import json
from pathlib import Path

from harness.artifacts.delivery_codes import DELIVERY_RETURN_CODE_BY_CODE, DELIVERY_RETURN_CODES
from harness.artifacts.validator import ArtifactValidator, delivery_issue_is_contract_only


def md_ok(text: str = "ok") -> str:
    return f"artifact_result_code: 0\n\n{text}"


def write_json_artifact(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_valid_plan_review_contracts(output_dir: Path) -> None:
    write_json_artifact(
        output_dir / "environment_contract.json",
        {
            "schema_version": "environment_contract.v1",
            "contract_id": "env",
            "contract_status": "final",
            "source": "test",
            "confidence": "unknown",
            "runtime": {"type": "unknown"},
            "setup": {"mode": "unknown", "commands": [], "discovery_allowed": True},
            "unknowns": [],
            "evidence_sources": [],
        },
    )
    write_json_artifact(
        output_dir / "validation_contract.json",
        {
            "schema_version": "validation_contract.v1",
            "contract_id": "validation",
            "contract_status": "final",
            "source": "test",
            "confidence": "unknown",
            "runtime": "unknown",
            "tests": {"mode": "unknown", "commands": [], "discovery_allowed": True},
            "pass_criteria": {"type": "unknown", "conditions": []},
            "acceptance_oracle_ids": [],
            "unknowns": [],
            "evidence_sources": [],
        },
    )


def test_artifact_validator_reports_missing_outputs(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "risk.md"])
    result = validator.validate_required_outputs_result(tmp_path, ["plan.md", "risk.md"])

    assert not ok
    assert errors == ["Missing required output: risk.md"]
    assert result.ok is False
    assert result.issues[0].artifact == "risk.md"
    assert result.issues[0].code == "missing_required_output"


def test_artifact_validator_accepts_complete_outputs(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "risk.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "risk.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_repairs_missing_markdown_artifact_result_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\nDo the work.\n", encoding="utf-8")
    result = validator.validate_required_outputs_result(tmp_path, ["plan.md"])

    repaired = validator.repair_trivial_contract_issues(tmp_path, result)
    repaired_result = validator.validate_required_outputs_result(tmp_path, ["plan.md"])

    assert repaired == ["plan.md"]
    assert repaired_result.ok
    assert path.read_text(encoding="utf-8").startswith("artifact_result_code: 0\n\n# Plan")


def test_artifact_validator_rejects_deprecated_review_status_route_field(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    path = tmp_path / "review_result.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "review_decision_code": 0,
                "review_status": "approved",
                "summary": "approved with non-blocking findings",
                "findings": [],
                "required_changes": [],
                "environment_check": {
                    "attempted": False,
                    "status": "not_applicable",
                    "commands_run": [],
                    "fixable": False,
                    "blocking_reason": "",
                },
            }
        ),
        encoding="utf-8",
    )
    result = validator.validate_required_outputs_result(tmp_path, ["review_result.json"])

    repaired = validator.repair_trivial_contract_issues(tmp_path, result)

    assert repaired == []
    assert result.errors == ["review_result.json review_status is deprecated; route only with review_decision_code"]


def test_artifact_validator_canonicalizes_selected_plan_oracle_kind_aliases(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    path = tmp_path / "selected_plan.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "selected plan",
                "acceptance_oracles": [
                    {
                        "id": "AO-1",
                        "description": "run existing regression",
                        "kind": "regression",
                        "required": True,
                        "commands": ["python -m pytest tests/test_example.py"],
                        "expected_exception": "",
                        "must_contain": [],
                        "must_not_contain": [],
                        "semantic_assertions": [],
                        "failure_signal": "pytest exits non-zero",
                        "evidence_hint": "pytest output",
                    },
                    {
                        "id": "AO-2",
                        "description": "compile project",
                        "kind": "compile",
                        "required": True,
                        "commands": ["python -m compileall -q ."],
                        "expected_exception": "",
                        "must_contain": [],
                        "must_not_contain": [],
                        "semantic_assertions": [],
                        "failure_signal": "compileall exits non-zero",
                        "evidence_hint": "compileall output",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    result = validator.validate_required_outputs_result(tmp_path, ["selected_plan.json"])

    repaired = validator.repair_trivial_contract_issues(tmp_path, result)
    repaired_payload = json.loads(path.read_text(encoding="utf-8"))
    repaired_result = validator.validate_required_outputs_result(tmp_path, ["selected_plan.json"])

    assert repaired == ["selected_plan.json"]
    assert [oracle["kind"] for oracle in repaired_payload["acceptance_oracles"]] == ["test", "test"]
    assert "acceptance_oracles[0].kind 'regression' -> 'test'" in repaired_payload["harness_canonicalizations"]
    assert "acceptance_oracles[1].kind 'compile' -> 'test'" in repaired_payload["harness_canonicalizations"]
    assert repaired_result.ok


def test_plan_review_approval_requires_nonblocking_findings_in_selected_plan(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    write_valid_plan_review_contracts(tmp_path)
    write_json_artifact(
        tmp_path / "review_result.json",
        {
            "schema_version": 1,
            "review_decision_code": 0,
            "summary": "acceptable with a note",
            "findings": [{"id": "F-1", "summary": "preserve stricter acceptance oracle"}],
            "required_changes": [],
            "acceptance_oracle_changes": [],
            "environment_check": {
                "attempted": False,
                "status": "not_applicable",
                "commands_run": [],
                "fixable": False,
                "blocking_reason": "",
            },
        },
    )
    write_json_artifact(
        tmp_path / "selected_plan.json",
        {
            "schema_version": 1,
            "summary": "selected plan",
            "acceptance_oracles": [
                {
                    "id": "AO-1",
                    "description": "run regression",
                    "kind": "test",
                    "required": True,
                    "commands": ["python -m pytest"],
                    "expected_exception": "",
                    "must_contain": [],
                    "must_not_contain": [],
                    "semantic_assertions": [],
                    "failure_signal": "pytest exits non-zero",
                    "evidence_hint": "pytest output",
                }
            ],
        },
    )

    result = validator.validate_required_outputs_result(
        tmp_path,
        ["review_result.json", "selected_plan.json", "environment_contract.json", "validation_contract.json"],
    )

    assert not result.ok
    assert any("requires selected_plan.json to carry them forward" in error for error in result.errors)

    selected_plan = json.loads((tmp_path / "selected_plan.json").read_text(encoding="utf-8"))
    selected_plan["reviewer_integrated_findings"] = ["F-1 preserved as AO-1"]
    write_json_artifact(tmp_path / "selected_plan.json", selected_plan)

    repaired_result = validator.validate_required_outputs_result(
        tmp_path,
        ["review_result.json", "selected_plan.json", "environment_contract.json", "validation_contract.json"],
    )

    assert repaired_result.ok


def test_plan_review_approval_rejects_required_changes(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    write_valid_plan_review_contracts(tmp_path)
    write_json_artifact(
        tmp_path / "review_result.json",
        {
            "schema_version": 1,
            "review_decision_code": 0,
            "summary": "incorrectly approved with required changes",
            "findings": [],
            "required_changes": ["planner must revise acceptance contract"],
            "acceptance_oracle_changes": [],
            "environment_check": {
                "attempted": False,
                "status": "not_applicable",
                "commands_run": [],
                "fixable": False,
                "blocking_reason": "",
            },
        },
    )
    write_json_artifact(
        tmp_path / "selected_plan.json",
        {
            "schema_version": 1,
            "summary": "selected plan",
            "reviewer_integrated_findings": ["note carried forward"],
            "acceptance_oracles": [
                {
                    "id": "AO-1",
                    "description": "run regression",
                    "kind": "test",
                    "required": True,
                    "commands": ["python -m pytest"],
                    "expected_exception": "",
                    "must_contain": [],
                    "must_not_contain": [],
                    "semantic_assertions": [],
                    "failure_signal": "pytest exits non-zero",
                    "evidence_hint": "pytest output",
                }
            ],
        },
    )

    result = validator.validate_required_outputs_result(
        tmp_path,
        ["review_result.json", "selected_plan.json", "environment_contract.json", "validation_contract.json"],
    )

    assert not result.ok
    assert any("required_changes to be empty" in error for error in result.errors)


def test_artifact_validator_accepts_json_delivery_return_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text(
        json.dumps(
            {
                "return_code": 0,
                "task_status": "success",
                "role_return_code": 0,
                "produced_files": ["plan.md", "delivery.md"],
                "known_risks": [],
            }
        ),
        encoding="utf-8",
    )

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_accepts_blank_lines_before_raw_delivery_return_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("\n\nreturn_code: 0\n# Delivery\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_rejects_empty_required_output(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "merged_patch.diff").write_text("", encoding="utf-8")
    (tmp_path / "merged_patch_metadata.json").write_text(
        json.dumps({"patch_artifact": "merged_patch.diff", "changed_files": [], "merge_report": {}}),
        encoding="utf-8",
    )
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(
        tmp_path, ["merged_patch.diff", "merged_patch_metadata.json", "delivery.md"]
    )

    assert not ok
    assert errors == ["Required output is empty: merged_patch.diff"]


def test_artifact_validator_allows_empty_fix_patch_for_noop_fix(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "fix_patch.diff").write_text("", encoding="utf-8")
    (tmp_path / "fix_schedule.md").write_text(md_ok("no-op schedule"), encoding="utf-8")
    (tmp_path / "fix_notes.md").write_text(md_ok("no_op_fix: true"), encoding="utf-8")
    (tmp_path / "self_check.md").write_text(md_ok("tester_result passed"), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(
        tmp_path, ["fix_schedule.md", "fix_patch.diff", "fix_notes.md", "self_check.md", "delivery.md"]
    )

    assert ok
    assert errors == []


def test_artifact_validator_accepts_delivery_return_code_anywhere(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("# Delivery\n\nreturn_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_accepts_bold_delivery_return_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("**return_code**: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_rejects_return_code_with_extra_text(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0 - complete\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["delivery.md must contain `return_code: <int>`"]


def test_artifact_validator_rejects_non_zero_delivery_return_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 1\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["delivery.md reports non-zero return_code: 1"]


def test_artifact_validator_rejects_legacy_delivery_status(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("status: success\n", encoding="utf-8")

    result = validator.validate_required_outputs_result(tmp_path, ["plan.md", "delivery.md"])
    ok, errors = result.ok, result.errors

    assert not ok
    assert errors == ["delivery.md must contain `return_code: <int>`"]
    assert delivery_issue_is_contract_only(result)


def test_artifact_validator_rejects_delivery_without_return_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("# Delivery\nNo status here.\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["delivery.md must contain `return_code: <int>`"]


def test_artifact_validator_rejects_markdown_without_artifact_result_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["plan.md must contain `artifact_result_code: <int>`"]


def test_artifact_validator_accepts_markdown_artifact_result_code_anywhere(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("# Plan\n\nartifact_result_code: 0\n\nBody\n", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_accepts_bold_markdown_artifact_result_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("# Plan\n\n- **artifact_result_code**: **0**\n\nBody\n", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_rejects_non_zero_markdown_artifact_result_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("artifact_result_code: 1\n\n# Plan\n", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["plan.md reports non-zero artifact_result_code: 1"]


def test_parse_delivery_status_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert ArtifactValidator().parse_delivery_status(tmp_path / "delivery.md") is None


def test_parse_delivery_status_maps_return_codes(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    delivery = tmp_path / "delivery.md"

    delivery.write_text("return_code: 0\n", encoding="utf-8")
    assert validator.parse_delivery_status(delivery) == "success"

    delivery.write_text("return_code: 2\n", encoding="utf-8")
    assert validator.parse_delivery_status(delivery) == "partial"

    delivery.write_text("return_code: -3\n", encoding="utf-8")
    assert validator.parse_delivery_status(delivery) == "failed"


def test_validator_rejects_selected_plan_without_acceptance_oracles(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "selected_plan.json").write_text(
        json.dumps({"schema_version": 1, "selected_plan_id": "plan", "summary": "do it"}),
        encoding="utf-8",
    )

    ok, errors = validator.validate_required_outputs(tmp_path, ["selected_plan.json"])

    assert not ok
    assert errors == ["selected_plan.json.acceptance_oracles must be a non-empty list"]


def test_validator_accepts_selected_plan_with_acceptance_oracles(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "selected_plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selected_plan_id": "plan",
                "summary": "do it",
                "acceptance_oracles": [
                    {
                        "id": "A1",
                        "description": "expected behavior is preserved",
                        "kind": "runtime",
                        "required": True,
                        "commands": ["pytest"],
                        "expected_exception": "",
                        "must_contain": [],
                        "must_not_contain": ["Traceback"],
                        "semantic_assertions": [],
                        "failure_signal": "pytest fails",
                        "evidence_hint": "pytest output",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    ok, errors = validator.validate_required_outputs(tmp_path, ["selected_plan.json"])

    assert ok
    assert errors == []


def test_validator_rejects_tester_result_without_oracle_results(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "tester_result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "tests_passed",
                "next_action": "continue",
                "failure_type": "none",
                "environment_dependency_issue": False,
                "summary": "passed",
                "setup_commands_run": [],
                "test_commands_run": [],
                "remaining_blockers": [],
            }
        ),
        encoding="utf-8",
    )

    ok, errors = validator.validate_required_outputs(tmp_path, ["tester_result.json"])

    assert not ok
    assert errors == ["tester_result.json.oracle_results must be a list"]


def test_validator_rejects_contract_command_section_without_mode(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "environment_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "environment_contract.v1",
                "contract_id": "env",
                "contract_status": "final",
                "source": "test",
                "confidence": "high",
                "runtime": {"type": "local"},
                "setup": {"commands": []},
                "unknowns": [],
                "evidence_sources": [],
            }
        ),
        encoding="utf-8",
    )

    ok, errors = validator.validate_required_outputs(tmp_path, ["environment_contract.json"])

    assert not ok
    assert any(error.startswith("environment_contract.json.setup.mode must be one of:") for error in errors)


def test_validator_rejects_explicit_validation_contract_with_empty_commands(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "validation_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "validation_contract.v1",
                "contract_id": "validation",
                "contract_status": "final",
                "source": "test",
                "confidence": "high",
                "runtime": "local",
                "tests": {"mode": "explicit", "commands": [], "discovery_allowed": True},
                "pass_criteria": {"type": "commands_exit_zero", "conditions": []},
                "acceptance_oracle_ids": [],
                "unknowns": [],
                "evidence_sources": [],
            }
        ),
        encoding="utf-8",
    )

    ok, errors = validator.validate_required_outputs(tmp_path, ["validation_contract.json"])

    assert not ok
    assert errors == ["validation_contract.json.tests.commands must be non-empty when mode is explicit"]


def test_validator_accepts_unknown_contract_mode_with_empty_commands(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "validation_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "validation_contract.v1",
                "contract_id": "validation",
                "contract_status": "final",
                "source": "test",
                "confidence": "unknown",
                "runtime": "unknown",
                "tests": {"mode": "unknown", "commands": [], "discovery_allowed": True},
                "pass_criteria": {"type": "unknown", "conditions": []},
                "acceptance_oracle_ids": [],
                "unknowns": ["test command not specified"],
                "evidence_sources": [],
            }
        ),
        encoding="utf-8",
    )

    ok, errors = validator.validate_required_outputs(tmp_path, ["validation_contract.json"])

    assert ok
    assert errors == []


def test_delivery_return_codes_have_canonical_meanings() -> None:
    assert {entry.code for entry in DELIVERY_RETURN_CODES} == {0, 1, 2, 3, -1, -2, -3}
    assert DELIVERY_RETURN_CODE_BY_CODE[0].status == "success"
    assert DELIVERY_RETURN_CODE_BY_CODE[1].label == "partial"
    assert DELIVERY_RETURN_CODE_BY_CODE[2].label == "blocked"
    assert DELIVERY_RETURN_CODE_BY_CODE[3].label == "degraded"
    assert DELIVERY_RETURN_CODE_BY_CODE[-1].label == "unusable"
    assert DELIVERY_RETURN_CODE_BY_CODE[-2].label == "invalid_outputs"
    assert DELIVERY_RETURN_CODE_BY_CODE[-3].label == "runtime_error"
