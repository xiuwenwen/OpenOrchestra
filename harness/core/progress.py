from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class ProgressEvent:
    event_type: str
    task_id: str
    phase: str | None = None
    role: str | None = None
    agent_id: str | None = None
    round_id: int | None = None
    attempt: int | None = None
    status: str | None = None
    message: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


ProgressCallback = Callable[[ProgressEvent], None]


class ProgressMultiplexer:
    def __init__(self, callbacks: list[ProgressCallback]):
        self.callbacks = callbacks

    def __call__(self, event: ProgressEvent) -> None:
        for callback in self.callbacks:
            callback(event)
