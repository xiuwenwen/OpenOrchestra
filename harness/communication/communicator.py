from __future__ import annotations

from pathlib import Path

from harness.state.repository import StateRepository


class Communicator:
    def __init__(self, repository: StateRepository):
        self.repository = repository

    def latest_final_delivery(self, task_id: str) -> Path | None:
        artifacts = self.repository.list_artifacts(task_id, "final_delivery.md")
        return self._latest_existing_path(artifacts)

    def latest_usage_guide(self, task_id: str) -> Path | None:
        artifacts = self.repository.list_artifacts(task_id, "usage_guide.md")
        return self._latest_existing_path(artifacts)

    def _latest_existing_path(self, artifacts: list[dict]) -> Path | None:
        for artifact in reversed(artifacts):
            path = Path(artifact["path"])
            if path.exists() and path.is_file():
                return path
        return None
