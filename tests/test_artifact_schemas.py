from __future__ import annotations

from pathlib import Path

from harness.artifacts.schemas import ARTIFACT_VISIBILITY_RULES
from harness.contracts.role_contracts import (
    artifact_input_budget_for,
    output_contract_lines_for,
    required_outputs_for,
    role_contract_for,
    role_instruction_for,
    role_phase_contract_for,
)


def test_required_outputs_always_include_delivery_envelope() -> None:
    assert required_outputs_for("tester", "TESTING") == [
        "bug_report.md",
        "tester_result.json",
        "delivery.md",
    ]


def test_output_contract_lines_for_tester_keep_verdict_codes_separate() -> None:
    lines = output_contract_lines_for("tester", "TESTING", required_outputs_for("tester", "TESTING"))
    text = "\n".join(lines)

    assert "`delivery.md` must be exactly one JSON object" in text
    assert "JSON `return_code` must be `0`" in text
    assert "Do not copy `build_result_code`, `test_result_code`, or `bug_result_code` values" in text
    assert "build_result_code: -1" in text
    assert "`artifact_result_code: 0` somewhere in the file" in text


def test_output_contract_lines_for_executor_do_not_define_test_verdicts() -> None:
    lines = output_contract_lines_for("executor", "EXECUTION", required_outputs_for("executor", "EXECUTION"))
    text = "\n".join(lines)

    assert "For executor Markdown notes and metadata" in text
    assert "build_result_code" not in text
    assert "review_decision_code" not in text


def test_visibility_rules_live_in_artifact_schema_layer() -> None:
    covered = {(rule.target_role, rule.target_phase) for rule in ARTIFACT_VISIBILITY_RULES}

    assert ("reviewer", "REVIEWING") in covered
    assert ("executor", "EXECUTION") in covered
    assert ("judge", "TEST_JUDGEMENT") in covered


def test_role_phase_contract_binds_outputs_and_visibility() -> None:
    contract = role_phase_contract_for("judge", "TEST_JUDGEMENT")

    assert contract.required_outputs_with_delivery() == ["decision.json", "decision_summary.md", "delivery.md"]
    assert {(rule.source_role, tuple(sorted(rule.artifact_types))) for rule in contract.visibility_rules} == {
        ("orchestrator", ("objective_gate.md",)),
        ("tester", ("bug_report.md", "tester_result.json")),
    }


def test_role_contract_registry_binds_instruction_outputs_and_contract_lines() -> None:
    contract = role_contract_for("feature_change", "tester", "TESTING")

    assert contract.role_instruction == role_instruction_for("tester", "feature_change")
    assert contract.required_outputs == ("bug_report.md", "tester_result.json", "delivery.md")
    assert contract.input_budget == artifact_input_budget_for("tester", "TESTING")
    assert any("Do not copy `build_result_code`" in line for line in contract.output_contract_lines)


def test_role_phase_input_budgets_are_defined_for_context_heavy_roles() -> None:
    expected = {
        ("tester", "TESTING"): (8, "path_only"),
        ("tester", "REGRESSION_TESTING"): (8, "path_only"),
        ("judge", "TEST_JUDGEMENT"): (8, "auto"),
        ("reviewer", "REVIEWING"): (12, "truncated"),
        ("communicator", "DELIVERY"): (8, "path_only"),
    }

    for (role, phase), (max_files, large_artifact_mode) in expected.items():
        budget = artifact_input_budget_for(role, phase)
        assert budget.max_files == max_files
        assert budget.large_artifact_mode == large_artifact_mode


def test_orchestrator_no_longer_owns_role_instruction_contracts() -> None:
    source = Path("harness/core/orchestrator.py").read_text(encoding="utf-8")

    assert "ROLE_INSTRUCTIONS =" not in source
    assert "ROLE_INSTRUCTIONS_BY_WORKFLOW" not in source
