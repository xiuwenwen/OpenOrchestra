from __future__ import annotations

from harness.contracts.visibility_matrix import render_visibility_matrix


def test_visibility_matrix_renders_contract_table() -> None:
    text = render_visibility_matrix()

    assert "# Generated Role/Phase Visibility Matrix" in text
    assert "| Target role | Target phase | Required outputs | Input budget | Source role |" in text
    assert "| judge | TEST_JUDGEMENT | `decision.json`, `decision_summary.md`, `delivery.md`" in text


def test_generated_visibility_matrix_documents_empty_tester_inputs() -> None:
    text = render_visibility_matrix()

    assert "| tester | TESTING | `bug_report.md`, `delivery.md`" in text
    assert "| tester | REGRESSION_TESTING | `bug_report.md`, `delivery.md`" in text
    assert "large_artifact_mode=path_only | (none) | (none)" in text
