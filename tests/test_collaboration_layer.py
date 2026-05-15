from __future__ import annotations

import pytest

from harness.collaboration.blackboard import CollaborationBlackboard
from harness.collaboration.consensus import DECISION_APPROVED, DECISION_BLOCKED, tally_consensus
from harness.collaboration.evidence_graph import EvidenceGraph
from harness.collaboration.messages import CollaborationMessage
from harness.collaboration.protocol import (
    STEP_CRITIQUE,
    STEP_MERGE,
    STEP_PROPOSE,
    code_collaboration_protocol,
    protocol_prompt_lines,
)


def _message(
    *,
    from_agent_id: str = "agent-1",
    to_agent_id: str | None = None,
    intent: str = "propose",
    claim: str = "approved",
    evidence_artifact_ids: tuple[str, ...] = ("artifact-1",),
    expires_at_round: int | None = None,
) -> CollaborationMessage:
    return CollaborationMessage(
        task_id="task-1",
        phase_id="phase-1",
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        intent=intent,
        claim=claim,
        evidence_artifact_ids=evidence_artifact_ids,
        requested_action=None,
        expires_at_round=expires_at_round,
    )


def test_collaboration_message_round_trips_and_validates_intent() -> None:
    message = _message(intent="CRITIQUE", claim="missing test evidence", to_agent_id=None)
    payload = message.to_dict()

    recovered = CollaborationMessage.from_dict(payload)

    assert recovered == message
    assert recovered.intent == "critique"
    assert recovered.is_broadcast
    assert recovered.message_id == message.message_id
    with pytest.raises(ValueError, match="Unsupported collaboration intent"):
        _message(intent="chat")


def test_blackboard_filters_by_phase_recipient_and_round_expiry() -> None:
    broadcast = _message(intent="ask", claim="need evidence", expires_at_round=2)
    direct = _message(from_agent_id="agent-2", to_agent_id="agent-1", intent="answer", claim="see artifact-2")
    expired = _message(from_agent_id="agent-3", intent="critique", claim="old critique", expires_at_round=0)
    blackboard = CollaborationBlackboard([broadcast, direct, expired])

    visible = blackboard.query(task_id="task-1", to_agent_id="agent-1", round_id=1)

    assert visible == (broadcast, direct)
    assert blackboard.by_artifact("artifact-1") == (broadcast, direct, expired)
    assert CollaborationBlackboard.from_jsonable(blackboard.to_jsonable()).all() == blackboard.all()


def test_code_collaboration_protocol_exposes_generic_steps() -> None:
    protocol = code_collaboration_protocol()

    assert protocol.steps == ("PROPOSE", "CRITIQUE", "REVISE", "VOTE", "MERGE")
    assert protocol.next_step(STEP_PROPOSE) == STEP_CRITIQUE
    assert protocol.next_step(STEP_MERGE) is None
    assert "critique" in protocol.intents_for(STEP_CRITIQUE)
    assert "PROPOSE -> CRITIQUE -> REVISE -> VOTE -> MERGE" in "\n".join(protocol_prompt_lines(STEP_MERGE))


def test_consensus_tally_uses_votes_and_block_veto() -> None:
    approved = _message(from_agent_id="agent-1", intent="vote", claim="approved")
    approved_2 = _message(from_agent_id="agent-2", intent="vote", claim="approve")
    changed = _message(from_agent_id="agent-3", intent="vote", claim="changes_required")

    assert tally_consensus([approved, approved_2, changed]).decision == DECISION_APPROVED
    assert tally_consensus([approved, _message(from_agent_id="agent-3", intent="block", claim="unsafe")]).decision == DECISION_BLOCKED


def test_evidence_graph_links_messages_to_artifacts() -> None:
    message = _message(evidence_artifact_ids=("artifact-1", "artifact-2"))
    graph = EvidenceGraph.from_messages([message])

    assert graph.artifact_ids_for_message(message.message_id) == ("artifact-1", "artifact-2")
    assert graph.message_ids_for_artifact("artifact-1") == (message.message_id,)
    assert graph.has_evidence_for_message(message.message_id)
