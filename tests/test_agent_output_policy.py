from __future__ import annotations

from harness.agents.output_policy import AgentOutputPolicy


def test_output_policy_collects_only_completed_valid_outputs() -> None:
    policy = AgentOutputPolicy()

    assert policy.should_collect_artifacts(agent_status="COMPLETED", validation_ok=True) is True
    assert policy.should_collect_artifacts(agent_status="COMPLETED", validation_ok=False) is False
    assert policy.should_collect_artifacts(agent_status="FAILED", validation_ok=True) is False


def test_output_policy_names_invalid_contract_status_explicitly() -> None:
    policy = AgentOutputPolicy()

    assert policy.invalid_output_status(validation_ok=False, agent_status="COMPLETED") == "OUTPUT_INVALID"
    assert policy.invalid_output_status(validation_ok=True, agent_status="FAILED") == "FAILED"
    assert policy.invalid_output_status(validation_ok=False, agent_status="FAILED") == "FAILED"
