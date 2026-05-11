from __future__ import annotations

from pathlib import Path


def test_workflow_engine_uses_runtime_contract_not_orchestrator_private_api() -> None:
    source = Path("harness/workflow/engine.py").read_text(encoding="utf-8")

    forbidden = ("self.orchestrator", "self.runtime._", "o._")

    assert not any(pattern in source for pattern in forbidden)


def test_agent_phase_runner_uses_public_runtime_api() -> None:
    source = Path("harness/agents/runner.py").read_text(encoding="utf-8")

    forbidden = ("o._", "orchestrator._", "self.orchestrator._")

    assert not any(pattern in source for pattern in forbidden)


def test_production_code_does_not_call_orchestrator_private_runtime_helpers() -> None:
    forbidden = ("o._", "orchestrator._", "runtime._", "self.runtime._")
    offenders: list[str] = []

    for path in Path("harness").rglob("*.py"):
        if path.as_posix() == "harness/core/orchestrator.py":
            continue
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            if pattern in source:
                offenders.append(f"{path}:{pattern}")

    assert offenders == []
