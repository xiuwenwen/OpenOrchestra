from __future__ import annotations

from pathlib import Path


def test_workflow_engine_uses_runtime_contract_not_orchestrator_private_api() -> None:
    source = Path("harness/workflow/engine.py").read_text(encoding="utf-8")

    forbidden = ("self.orchestrator", "self.runtime._", "o._")

    assert not any(pattern in source for pattern in forbidden)
