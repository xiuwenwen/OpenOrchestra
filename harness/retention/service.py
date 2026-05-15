from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from harness.state.repository import StateRepository


CLEANABLE_TASK_STATUSES = {"CREATED", "PENDING", "COMPLETED", "FAILED"}


@dataclass(frozen=True)
class RetentionAction:
    path: Path
    size: int = 0
    action: str = "skip"
    reason: str = ""


@dataclass(frozen=True)
class RetentionPlan:
    task_id: str
    dry_run: bool
    success_path: Path | None
    final_delivery: Path | None
    response: Path | None
    deleted: list[RetentionAction]
    skipped: list[RetentionAction]

    @property
    def freed_bytes(self) -> int:
        return sum(action.size for action in self.deleted)


class RetentionService:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        repository: StateRepository,
        success_path_provider: Callable[[str], Path | None] | None = None,
    ):
        self.config = config
        self.repository = repository
        self.success_path_provider = success_path_provider

    def clean_task(self, task_id: str, *, dry_run: bool = False) -> RetentionPlan:
        task = self.repository.get_task(task_id)
        if not task:
            return RetentionPlan(task_id, dry_run, None, None, None, [], [RetentionAction(Path(task_id), reason="task not found")])
        success_path = self.success_path(task_id)
        final_delivery = success_path / "final_delivery.json" if success_path else None
        response = self.latest_artifact_path(task_id, "response.md")
        if str(task["status"]) not in CLEANABLE_TASK_STATUSES:
            return self.refusal_plan(task_id, dry_run, success_path, final_delivery, response, "task is active")
        if not success_path or not success_path.exists():
            return self.refusal_plan(task_id, dry_run, success_path, final_delivery, response, "no final success_path found")
        if (not final_delivery or not final_delivery.exists()) and not response:
            return self.refusal_plan(task_id, dry_run, success_path, final_delivery, response, "no final delivery or response found")

        candidates = [
            Path(self.config["system"]["workspace_root"]).expanduser().resolve() / task_id,
            Path(self.config["system"]["artifact_root"]).expanduser().resolve() / task_id,
        ]
        deleted: list[RetentionAction] = []
        skipped: list[RetentionAction] = []
        for path in candidates:
            if not path.exists():
                skipped.append(RetentionAction(path, reason="not found"))
                continue
            if not self.is_safe_clean_target(path, task_id, success_path):
                skipped.append(RetentionAction(path, reason="unsafe target"))
                continue
            size = path_size(path)
            action = RetentionAction(path, size=size, action="dry_run" if dry_run else "delete")
            deleted.append(action)
            if not dry_run:
                shutil.rmtree(path)
        return RetentionPlan(task_id, dry_run, success_path, final_delivery, response, deleted, skipped)

    def refusal_plan(
        self,
        task_id: str,
        dry_run: bool,
        success_path: Path | None,
        final_delivery: Path | None,
        response: Path | None,
        reason: str,
    ) -> RetentionPlan:
        return RetentionPlan(
            task_id,
            dry_run,
            success_path,
            final_delivery,
            response,
            [],
            [RetentionAction(Path(task_id), reason=reason)],
        )

    def success_path(self, task_id: str) -> Path | None:
        success_path_md = self.latest_artifact_path(task_id, "success_path.md")
        if success_path_md:
            return success_path_md.parent
        if self.success_path_provider:
            provided = self.success_path_provider(task_id)
            if provided:
                return provided
        final_delivery = self.latest_artifact_path(task_id, "final_delivery.json")
        return final_delivery.parent if final_delivery else None

    def latest_artifact_path(self, task_id: str, artifact_type: str) -> Path | None:
        for artifact in reversed(self.repository.list_artifacts(task_id, artifact_type)):
            path = Path(artifact["path"])
            if path.exists() and path.is_file():
                return path
        return None

    def is_safe_clean_target(self, path: Path, task_id: str, success_path: Path) -> bool:
        resolved = path.resolve()
        if resolved.name != task_id:
            return False
        return not path_contains(resolved, success_path.resolve())


def path_contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def format_bytes(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(size)} B"
