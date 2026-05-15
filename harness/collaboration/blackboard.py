from __future__ import annotations

from collections.abc import Iterable

from harness.collaboration.messages import CollaborationMessage


class CollaborationBlackboard:
    def __init__(self, messages: Iterable[CollaborationMessage] | None = None):
        self._messages: list[CollaborationMessage] = list(messages or ())

    def add(self, message: CollaborationMessage) -> CollaborationMessage:
        self._messages.append(message)
        return message

    def extend(self, messages: Iterable[CollaborationMessage]) -> None:
        self._messages.extend(messages)

    def all(self) -> tuple[CollaborationMessage, ...]:
        return tuple(self._messages)

    def query(
        self,
        *,
        task_id: str | None = None,
        phase_id: str | None = None,
        intent: str | None = None,
        from_agent_id: str | None = None,
        to_agent_id: str | None = None,
        include_broadcast: bool = True,
        round_id: int | None = None,
    ) -> tuple[CollaborationMessage, ...]:
        normalized_intent = intent.lower() if intent else None
        return tuple(
            message
            for message in self._messages
            if (task_id is None or message.task_id == task_id)
            and (phase_id is None or message.phase_id == phase_id)
            and (normalized_intent is None or message.intent == normalized_intent)
            and (from_agent_id is None or message.from_agent_id == from_agent_id)
            and self._matches_recipient(message, to_agent_id, include_broadcast)
            and message.is_active_for_round(round_id)
        )

    def by_artifact(self, artifact_id: str) -> tuple[CollaborationMessage, ...]:
        return tuple(message for message in self._messages if artifact_id in message.evidence_artifact_ids)

    def to_jsonable(self) -> list[dict[str, object]]:
        return [message.to_dict() for message in self._messages]

    @classmethod
    def from_jsonable(cls, payload: list[dict[str, object]]) -> "CollaborationBlackboard":
        return cls(CollaborationMessage.from_dict(item) for item in payload)

    def _matches_recipient(
        self,
        message: CollaborationMessage,
        to_agent_id: str | None,
        include_broadcast: bool,
    ) -> bool:
        if to_agent_id is None:
            return True
        return message.to_agent_id == to_agent_id or (include_broadcast and message.is_broadcast)
