from __future__ import annotations

from pathlib import Path


def test_delivery_publisher_uses_injected_ports_not_orchestrator_private_api() -> None:
    source = Path("harness/workflow/delivery.py").read_text(encoding="utf-8")

    assert "self.orchestrator" not in source
    assert "o._" not in source
    assert "latest_materialized_repo" in source
    assert "source_repo_for_existing_project_task" in source
