from __future__ import annotations

from harness.artifacts.schemas import ARTIFACT_VISIBILITY_RULES, output_contract_lines_for, required_outputs_for, role_phase_contract_for


def test_required_outputs_always_include_delivery_envelope() -> None:
    assert required_outputs_for("tester", "TESTING") == [
        "bug_report.md",
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
        ("orchestrator", ("objective_gate.md", "test_gate.md")),
        ("tester", ("bug_report.md",)),
    }
