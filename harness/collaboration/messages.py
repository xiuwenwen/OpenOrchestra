from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any


INTENT_PROPOSE = "propose"
INTENT_CRITIQUE = "critique"
INTENT_ASK = "ask"
INTENT_ANSWER = "answer"
INTENT_VOTE = "vote"
INTENT_BLOCK = "block"
VALID_INTENTS = frozenset(
    {
        INTENT_PROPOSE,
        INTENT_CRITIQUE,
        INTENT_ASK,
        INTENT_ANSWER,
        INTENT_VOTE,
        INTENT_BLOCK,
    }
)


@dataclass(frozen=True)
class CollaborationMessage:
    task_id: str
    phase_id: str
    from_agent_id: str
    to_agent_id: str | None
    intent: str
    claim: str
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    requested_action: str | None = None
    expires_at_round: int | None = None

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("CollaborationMessage.task_id is required")
        if not self.phase_id.strip():
            raise ValueError("CollaborationMessage.phase_id is required")
        if not self.from_agent_id.strip():
            raise ValueError("CollaborationMessage.from_agent_id is required")
        normalized_intent = self.intent.strip().lower()
        if normalized_intent not in VALID_INTENTS:
            raise ValueError(f"Unsupported collaboration intent: {self.intent!r}")
        if not self.claim.strip():
            raise ValueError("CollaborationMessage.claim is required")
        if self.expires_at_round is not None and self.expires_at_round < 0:
            raise ValueError("CollaborationMessage.expires_at_round must be non-negative")
        object.__setattr__(self, "intent", normalized_intent)
        object.__setattr__(self, "evidence_artifact_ids", tuple(self.evidence_artifact_ids))

    @property
    def is_broadcast(self) -> bool:
        return self.to_agent_id is None

    @property
    def message_id(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def is_active_for_round(self, round_id: int | None) -> bool:
        if round_id is None or self.expires_at_round is None:
            return True
        return round_id <= self.expires_at_round

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "phase_id": self.phase_id,
            "from_agent_id": self.from_agent_id,
            "to_agent_id": self.to_agent_id,
            "intent": self.intent,
            "claim": self.claim,
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "requested_action": self.requested_action,
            "expires_at_round": self.expires_at_round,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CollaborationMessage":
        return cls(
            task_id=str(payload.get("task_id") or ""),
            phase_id=str(payload.get("phase_id") or ""),
            from_agent_id=str(payload.get("from_agent_id") or ""),
            to_agent_id=_optional_string(payload.get("to_agent_id")),
            intent=str(payload.get("intent") or ""),
            claim=str(payload.get("claim") or ""),
            evidence_artifact_ids=tuple(str(item) for item in payload.get("evidence_artifact_ids") or ()),
            requested_action=_optional_string(payload.get("requested_action")),
            expires_at_round=_optional_int(payload.get("expires_at_round")),
        )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid round value")
    return int(value)
