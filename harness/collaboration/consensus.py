from __future__ import annotations

from dataclasses import dataclass

from harness.collaboration.messages import CollaborationMessage, INTENT_BLOCK, INTENT_VOTE


DECISION_APPROVED = "approved"
DECISION_CHANGES_REQUESTED = "changes_requested"
DECISION_BLOCKED = "blocked"
DECISION_NO_QUORUM = "no_quorum"


@dataclass(frozen=True)
class ConsensusPolicy:
    approval_threshold: float = 0.5
    block_is_veto: bool = True
    minimum_votes: int = 1


@dataclass(frozen=True)
class ConsensusResult:
    decision: str
    approvals: int
    changes_requested: int
    blocks: int
    voters: tuple[str, ...]
    blocking_message_ids: tuple[str, ...]

    @property
    def approved(self) -> bool:
        return self.decision == DECISION_APPROVED


def tally_consensus(
    messages: tuple[CollaborationMessage, ...] | list[CollaborationMessage],
    policy: ConsensusPolicy | None = None,
) -> ConsensusResult:
    policy = policy or ConsensusPolicy()
    latest_vote_by_agent: dict[str, CollaborationMessage] = {}
    block_messages: list[CollaborationMessage] = []
    for message in messages:
        if message.intent == INTENT_BLOCK:
            block_messages.append(message)
        if message.intent == INTENT_VOTE:
            latest_vote_by_agent[message.from_agent_id] = message
    vote_messages = tuple(latest_vote_by_agent.values())
    approvals = sum(1 for message in vote_messages if _normalized_vote(message.claim) == DECISION_APPROVED)
    changes_requested = sum(
        1 for message in vote_messages if _normalized_vote(message.claim) == DECISION_CHANGES_REQUESTED
    )
    blocks = len(block_messages) + sum(1 for message in vote_messages if _normalized_vote(message.claim) == DECISION_BLOCKED)
    voters = tuple(sorted(latest_vote_by_agent))
    blocking_ids = tuple(message.message_id for message in block_messages)
    if blocks and policy.block_is_veto:
        decision = DECISION_BLOCKED
    elif len(vote_messages) < policy.minimum_votes:
        decision = DECISION_NO_QUORUM
    elif vote_messages and approvals / len(vote_messages) > policy.approval_threshold:
        decision = DECISION_APPROVED
    elif changes_requested or blocks:
        decision = DECISION_CHANGES_REQUESTED
    else:
        decision = DECISION_NO_QUORUM
    return ConsensusResult(
        decision=decision,
        approvals=approvals,
        changes_requested=changes_requested,
        blocks=blocks,
        voters=voters,
        blocking_message_ids=blocking_ids,
    )


def _normalized_vote(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"approve", "approved", "pass", "accepted"}:
        return DECISION_APPROVED
    if normalized in {"changes", "changes_required", "revise", "revision_required", "reject"}:
        return DECISION_CHANGES_REQUESTED
    if normalized in {"block", "blocked", "blocking"}:
        return DECISION_BLOCKED
    return normalized
