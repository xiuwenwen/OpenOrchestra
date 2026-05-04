from __future__ import annotations

from harness.artifacts.schemas import required_outputs_for


def delivery_required_outputs() -> list[str]:
    return required_outputs_for("communicator", "DELIVERY")

