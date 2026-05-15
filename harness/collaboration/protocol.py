from __future__ import annotations

from dataclasses import dataclass

from harness.collaboration.messages import (
    INTENT_ANSWER,
    INTENT_ASK,
    INTENT_BLOCK,
    INTENT_CRITIQUE,
    INTENT_PROPOSE,
    INTENT_VOTE,
)


STEP_PROPOSE = "PROPOSE"
STEP_CRITIQUE = "CRITIQUE"
STEP_REVISE = "REVISE"
STEP_VOTE = "VOTE"
STEP_MERGE = "MERGE"
CODE_COLLABORATION_STEPS = (STEP_PROPOSE, STEP_CRITIQUE, STEP_REVISE, STEP_VOTE, STEP_MERGE)


@dataclass(frozen=True)
class CollaborationProtocol:
    name: str
    steps: tuple[str, ...]
    step_intents: dict[str, tuple[str, ...]]

    def next_step(self, current_step: str) -> str | None:
        try:
            index = self.steps.index(current_step)
        except ValueError:
            raise ValueError(f"Unknown collaboration step: {current_step}") from None
        if index + 1 >= len(self.steps):
            return None
        return self.steps[index + 1]

    def intents_for(self, step: str) -> tuple[str, ...]:
        if step not in self.step_intents:
            raise ValueError(f"Unknown collaboration step: {step}")
        return self.step_intents[step]

    def prompt_lines(self) -> list[str]:
        return [
            f"- Collaboration protocol: {' -> '.join(self.steps)}.",
            "- Agents exchange structured claims through artifacts, not direct free-form chat.",
            "- Each collaboration claim should cite evidence artifact ids when available.",
        ]


def code_collaboration_protocol() -> CollaborationProtocol:
    return CollaborationProtocol(
        name="code_collaboration",
        steps=CODE_COLLABORATION_STEPS,
        step_intents={
            STEP_PROPOSE: (INTENT_PROPOSE,),
            STEP_CRITIQUE: (INTENT_CRITIQUE, INTENT_ASK, INTENT_BLOCK),
            STEP_REVISE: (INTENT_PROPOSE, INTENT_ANSWER),
            STEP_VOTE: (INTENT_VOTE, INTENT_BLOCK),
            STEP_MERGE: (INTENT_PROPOSE,),
        },
    )


def protocol_prompt_lines(current_step: str | None = None) -> list[str]:
    protocol = code_collaboration_protocol()
    lines = protocol.prompt_lines()
    if current_step:
        lines.append(f"- Current collaboration step: {current_step}.")
        lines.append(f"- Allowed message intents for this step: {', '.join(protocol.intents_for(current_step))}.")
    return lines
