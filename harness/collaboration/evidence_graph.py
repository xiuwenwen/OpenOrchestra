from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import DefaultDict

from harness.collaboration.messages import CollaborationMessage


@dataclass(frozen=True)
class EvidenceEdge:
    message_id: str
    artifact_id: str
    relation: str = "cites"


class EvidenceGraph:
    def __init__(self, edges: tuple[EvidenceEdge, ...] | None = None):
        self.edges = edges or ()
        self._artifacts_by_message: DefaultDict[str, list[str]] = defaultdict(list)
        self._messages_by_artifact: DefaultDict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            self._artifacts_by_message[edge.message_id].append(edge.artifact_id)
            self._messages_by_artifact[edge.artifact_id].append(edge.message_id)

    @classmethod
    def from_messages(cls, messages: tuple[CollaborationMessage, ...] | list[CollaborationMessage]) -> "EvidenceGraph":
        return cls(
            tuple(
                EvidenceEdge(message.message_id, artifact_id)
                for message in messages
                for artifact_id in message.evidence_artifact_ids
            )
        )

    def artifact_ids_for_message(self, message_id: str) -> tuple[str, ...]:
        return tuple(self._artifacts_by_message.get(message_id, ()))

    def message_ids_for_artifact(self, artifact_id: str) -> tuple[str, ...]:
        return tuple(self._messages_by_artifact.get(artifact_id, ()))

    def has_evidence_for_message(self, message_id: str) -> bool:
        return bool(self._artifacts_by_message.get(message_id))
