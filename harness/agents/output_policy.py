from __future__ import annotations


class AgentOutputPolicy:
    """Decides whether an agent attempt is allowed to publish visible artifacts."""

    def should_collect_artifacts(self, *, agent_status: str, validation_ok: bool) -> bool:
        return agent_status == "COMPLETED" and validation_ok

    def invalid_output_status(self, *, validation_ok: bool, agent_status: str) -> str:
        if validation_ok:
            return "FAILED"
        return "OUTPUT_INVALID"
