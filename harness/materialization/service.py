from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable

from harness.artifacts.hashing import sha256_file
from harness.core.state_machine import FIXING, PLAN_REVIEW, REGRESSION_TESTING, REVIEW_FIXING, TESTING
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, NEW_PROJECT, normalize_workflow_type
from harness.state.repository import StateRepository
from harness.workspace.manager import WorkspaceManager


MATERIALIZED_SUCCESS_MARKER = ".harness_materialized_success.json"
MarkdownFieldReader = Callable[[str, str], str | None]


class MaterializedRepoService:
    def __init__(
        self,
        repository: StateRepository,
        workspace_manager: WorkspaceManager,
        *,
        config: dict[str, Any],
        markdown_field: MarkdownFieldReader,
        active_task_id: Callable[[], str | None],
        active_workflow_type: Callable[[], str | None],
    ):
        self.repository = repository
        self.workspace_manager = workspace_manager
        self.config = config
        self.markdown_field = markdown_field
        self.active_task_id = active_task_id
        self.active_workflow_type = active_workflow_type

    def source_repo_for_workspace(self) -> Path | None:
        task_id = self.active_task_id()
        if task_id:
            return self.source_repo_for_existing_project_task(task_id)
        if self.active_workflow_type() in {BUGFIX, FEATURE_CHANGE}:
            return self.configured_source_repo()
        return None

    def source_repo_for_existing_project_task(self, task_id: str) -> Path | None:
        if not self.task_uses_existing_project_source(task_id):
            return None
        return self.project_context_source_repo(task_id) or self.configured_source_repo()

    def task_uses_existing_project_source(self, task_id: str) -> bool:
        if self.active_task_id() == task_id and self.active_workflow_type():
            workflow_type = self.active_workflow_type()
        else:
            task = self.repository.get_task(task_id)
            workflow_type = str(task.get("workflow_type") or NEW_PROJECT) if task else NEW_PROJECT
        return normalize_workflow_type(workflow_type) in {BUGFIX, FEATURE_CHANGE}

    def project_context_source_repo(self, task_id: str) -> Path | None:
        for artifact in reversed(self.repository.list_artifacts(task_id, "project_context.md")):
            path = Path(artifact["path"])
            if not path.exists() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            for candidate in self.project_context_source_candidates(content):
                resolved = candidate.expanduser().resolve()
                if resolved.exists() and resolved.is_dir():
                    return resolved
        return None

    def project_context_source_candidates(self, content: str) -> list[Path]:
        explicit_source_paths: list[Path] = []
        success_source_paths: list[Path] = []
        fallback_repo_paths: list[Path] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            for prefix in ("Historical source_repo:", "- source_repo:"):
                value = self.context_line_value(line, prefix)
                if value:
                    explicit_source_paths.append(Path(value))
            for prefix in (
                "Historical materialized_source:",
                "Historical materialized_source_candidate:",
                "Historical partial_materialized_source:",
                "- materialized_source:",
                "- materialized_source_candidate:",
                "- partial_materialized_source:",
            ):
                value = self.context_line_value(line, prefix)
                if value:
                    explicit_source_paths.append(Path(value))
            for prefix in ("Historical success_path:", "- success_path:"):
                value = self.context_line_value(line, prefix)
                if value:
                    success_source_paths.append(Path(value) / "source")
            for prefix in ("Historical latest_agent_repo_workspace:", "- latest_agent_repo_workspace:"):
                value = self.context_line_value(line, prefix)
                if value:
                    fallback_repo_paths.append(Path(value))
        return explicit_source_paths + success_source_paths + fallback_repo_paths

    def context_line_value(self, line: str, prefix: str) -> str | None:
        if not line.startswith(prefix):
            return None
        value = line[len(prefix) :].strip()
        return value or None

    def should_use_materialized_repo(self, role: str, phase: str) -> bool:
        if role == "executor":
            return phase in {FIXING, REVIEW_FIXING}
        if role == "tester":
            return phase in {TESTING, REGRESSION_TESTING}
        if role == "reviewer":
            return phase != PLAN_REVIEW
        if role == "communicator":
            return True
        return False

    def prepare_workspace_repo(self, task_id: str, role: str, phase: str, repo_dir: Path) -> None:
        if not self.should_use_materialized_repo(role, phase):
            return
        materialized_repo = self.latest_materialized_repo(task_id)
        if not materialized_repo:
            return
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        self.workspace_manager.copytree(
            materialized_repo,
            repo_dir,
            ignore=self.copy_ignore_for_materialized_workspace,
        )
        self.workspace_manager.initialize_git_baseline(repo_dir)

    def repo_context_metadata(self, task_id: str, role: str, phase: str) -> dict[str, Any]:
        if self.should_use_materialized_repo(role, phase):
            materialized_repo = self.latest_materialized_repo(task_id)
            if materialized_repo:
                return {
                    "repository_source_type": "materialized_merged_patch",
                    "repository_source_path": str(materialized_repo),
                    "repository_source_note": "This role's repository directory was copied from the latest Harness materialized merged patch.",
                }
        project_context_source_repo = (
            self.project_context_source_repo(task_id)
            if self.task_uses_existing_project_source(task_id)
            else None
        )
        if project_context_source_repo:
            return {
                "repository_source_type": "project_context_source_repo",
                "repository_source_note": (
                    "This role's repository directory was copied from the source repo selected from project_context.md. "
                    "Use only the workspace Repository directory; the original source path is intentionally hidden."
                ),
            }
        source_repo = self.source_repo_for_workspace()
        if source_repo:
            return {
                "repository_source_type": "configured_source_repo",
                "repository_source_note": (
                    "This role's repository directory was copied from the configured source repo. "
                    "Use only the workspace Repository directory; the original source path is intentionally hidden."
                ),
            }
        return {"repository_source_type": "empty_workspace_repo"}

    def materialized_root(self, task_id: str) -> Path:
        return self.workspace_manager.workspace_root / task_id / "_materialized"

    def materialized_repo_dir(self, task_id: str, round_id: int) -> Path:
        return self.materialized_root(task_id) / f"round_{round_id}" / "repo"

    def latest_materialized_repo(self, task_id: str) -> Path | None:
        latest_success_round = self.latest_successful_materialized_round_from_artifacts(task_id)
        if latest_success_round is None:
            return None
        root = self.materialized_root(task_id)
        if not root.exists():
            return None
        candidate = self.materialized_repo_dir(task_id, latest_success_round)
        if not self.materialized_success_marker_ok(candidate, task_id, latest_success_round):
            return None
        return candidate

    def latest_successful_materialized_round_from_artifacts(self, task_id: str) -> int | None:
        artifacts = self.repository.list_artifacts(task_id, "materialized_repo.md")
        for artifact in reversed(artifacts):
            path = Path(artifact["path"])
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if self.materialized_repo_status(text) != "success":
                continue
            round_id = self.extract_materialized_report_round(text)
            if round_id is not None:
                return round_id
        return None

    def materialized_repo_status(self, report: str) -> str:
        return self.materialized_repo_field(report, "status") or "unknown"

    def materialized_repo_field(self, report: str, field_name: str) -> str | None:
        prefix = f"{field_name}: "
        for line in report.splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip().lower()
        return None

    def extract_materialized_report_round(self, report: str) -> int | None:
        for line in report.splitlines():
            if line.startswith("round_id: "):
                try:
                    return int(line.split(":", 1)[1].strip())
                except ValueError:
                    return None
        return None

    def materialized_success_marker_ok(self, repo_dir: Path, task_id: str, round_id: int) -> bool:
        marker_path = repo_dir / MATERIALIZED_SUCCESS_MARKER
        if not repo_dir.is_dir() or not marker_path.is_file():
            return False
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        return (
            payload.get("status") == "success"
            and payload.get("task_id") == task_id
            and int(payload.get("round_id", -1)) == round_id
        )

    def write_materialized_success_marker(self, repo_dir: Path, task_id: str, round_id: int, patch_path: Path) -> None:
        marker = {
            "status": "success",
            "task_id": task_id,
            "round_id": round_id,
            "patch_path": str(patch_path),
            "patch_hash": sha256_file(patch_path) if patch_path.exists() else None,
        }
        (repo_dir / MATERIALIZED_SUCCESS_MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def configured_source_repo(self) -> Path | None:
        source_repo = self.config.get("system", {}).get("source_repo")
        if not source_repo:
            return None
        resolved = Path(str(source_repo)).expanduser().resolve()
        return resolved if resolved.exists() and resolved.is_dir() else None

    def copy_source_for_patch_validation(self, source_repo: Path, destination: Path) -> None:
        self.workspace_manager.copytree(
            source_repo,
            destination,
            ignore=lambda directory, names: {
                name
                for name in names
                if self.should_ignore_copied_source_item(directory, name)
            },
        )

    def should_ignore_copied_source_item(self, directory: str, name: str) -> bool:
        path = (Path(directory) / name).resolve()
        return (
            name in WorkspaceManager.DEFAULT_COPY_IGNORE_NAMES
            or WorkspaceManager.is_generated_runtime_artifact(path)
            or self.is_relative_to(path, self.workspace_manager.workspace_root)
        )

    def copy_ignore_for_materialized_workspace(self, directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {
                ".git",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                MATERIALIZED_SUCCESS_MARKER,
            }
            or WorkspaceManager.is_generated_runtime_artifact((Path(directory) / name).resolve())
        }

    def is_relative_to(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False
