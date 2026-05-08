from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    user_prompt: str
    status: str
    current_phase: str | None
    current_role: str | None
    configuration: dict | None
    created_at: str
    updated_at: str

