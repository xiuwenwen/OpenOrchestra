from __future__ import annotations

from harness.architecture.metrics import (
    DEFAULT_DOC_PATH,
    collect_architecture_metrics,
    render_architecture_metrics,
    threshold_warnings,
)


def test_generated_architecture_metrics_are_current() -> None:
    assert DEFAULT_DOC_PATH.read_text(encoding="utf-8") == render_architecture_metrics()


def test_architecture_metrics_remain_within_wide_guardrails() -> None:
    assert threshold_warnings(collect_architecture_metrics()) == []
