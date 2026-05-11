from __future__ import annotations

from harness.architecture.metrics import (
    collect_architecture_metrics,
    render_architecture_metrics,
    threshold_warnings,
)


def test_architecture_metrics_render_contains_current_baseline() -> None:
    text = render_architecture_metrics()

    assert "# Generated Architecture Metrics" in text
    assert "Production total LOC" in text
    assert "Longest Production Functions" in text


def test_architecture_metrics_remain_within_wide_guardrails() -> None:
    assert threshold_warnings(collect_architecture_metrics()) == []
