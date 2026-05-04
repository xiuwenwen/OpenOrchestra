from __future__ import annotations

from harness.artifacts.schemas import required_outputs_for


def execution_required_outputs(phase: str = "EXECUTION") -> list[str]:
    return required_outputs_for("executor", phase)

