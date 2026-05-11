from __future__ import annotations

from harness.contracts.visibility_matrix import DEFAULT_DOC_PATH, render_visibility_matrix


def test_generated_visibility_matrix_is_current() -> None:
    assert DEFAULT_DOC_PATH.read_text(encoding="utf-8") == render_visibility_matrix()


def test_generated_visibility_matrix_documents_empty_tester_inputs() -> None:
    text = render_visibility_matrix()

    assert "| tester | TESTING | `bug_report.md`, `delivery.md`" in text
    assert "| tester | REGRESSION_TESTING | `bug_report.md`, `delivery.md`" in text
    assert "large_artifact_mode=path_only | (none) | (none)" in text
