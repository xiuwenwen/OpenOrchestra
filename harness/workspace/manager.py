from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None


@dataclass(frozen=True)
class WorkspacePaths:
    workspace_dir: Path
    input_dir: Path
    output_dir: Path
    log_dir: Path
    repo_dir: Path


class WorkspaceManager:
    LINUX_FICLONE = 0x40049409
    DEFAULT_COPY_IGNORE_NAMES = {
        ".git",
        ".mypy_cache",
        ".openorchestra-cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "artifacts",
        "deliver",
        "deliveries",
        "logs",
        "state",
        "workSpace",
        "workspaces",
    }
    RUNTIME_OUTPUT_ROOT_NAMES = {
        "output",
        "outputs",
        "reports",
        "test-results",
        "test_results",
    }
    RUNTIME_GENERATED_DIR_NAMES = {
        "screenshots",
        "screenshot",
        "videos",
        "video",
        "recordings",
        "recording",
        "traces",
        "trace",
    }
    RUNTIME_GENERATED_FILE_SUFFIXES = {
        ".avi",
        ".bmp",
        ".gif",
        ".jpeg",
        ".jpg",
        ".mov",
        ".mp4",
        ".png",
        ".tiff",
        ".webm",
        ".webp",
    }

    def __init__(self, workspace_root: str | Path):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def create_workspace(
        self,
        task_id: str,
        phase_id: str,
        role: str,
        agent_id: str,
        round_id: int,
        attempt: int,
        source_repo: str | Path | None = None,
    ) -> WorkspacePaths:
        workspace_dir = (
            self.workspace_root
            / task_id
            / phase_id
            / role
            / agent_id
            / f"round_{round_id}"
            / f"attempt_{attempt}"
        )
        input_dir = workspace_dir / "input"
        output_dir = workspace_dir / "output"
        log_dir = workspace_dir / "logs"
        repo_dir = workspace_dir / "repo"
        for path in (input_dir, output_dir, log_dir):
            path.mkdir(parents=True, exist_ok=False)
        if source_repo:
            self.copytree(source_repo, repo_dir, ignore=self._copy_ignore)
        else:
            repo_dir.mkdir(parents=True, exist_ok=False)
        return WorkspacePaths(workspace_dir, input_dir, output_dir, log_dir, repo_dir)

    def copytree(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        ignore: Callable[[str, list[str]], set[str]] | None = None,
    ) -> None:
        shutil.copytree(source, destination, ignore=ignore, copy_function=self.copy_file_fast)

    @classmethod
    def copy_file_fast(cls, source: str | Path, destination: str | Path) -> str:
        src = Path(source)
        dst = Path(destination)
        if src.is_symlink():
            return shutil.copy2(src, dst)
        if cls._clone_file(source, destination):
            shutil.copystat(source, destination, follow_symlinks=True)
            return str(destination)
        return shutil.copy2(source, destination)

    @classmethod
    def _clone_file(cls, source: str | Path, destination: str | Path) -> bool:
        clonefile = getattr(os, "clonefile", None)
        if clonefile is not None:
            try:
                clonefile(source, destination)
                return True
            except OSError:
                pass
        if fcntl is None:
            return False
        try:
            with open(source, "rb") as src_handle, open(destination, "wb") as dst_handle:
                fcntl.ioctl(dst_handle.fileno(), cls.LINUX_FICLONE, src_handle.fileno())
            return True
        except OSError:
            try:
                Path(destination).unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _copy_ignore(self, directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        base = Path(directory)
        for name in names:
            path = (base / name).resolve()
            if (
                name in self.DEFAULT_COPY_IGNORE_NAMES
                or self.is_generated_runtime_artifact(path)
                or self._is_relative_to(path, self.workspace_root)
            ):
                ignored.add(name)
        return ignored

    @classmethod
    def is_generated_runtime_artifact(cls, path: Path) -> bool:
        parts = tuple(part.lower() for part in path.parts)
        if not any(part in cls.RUNTIME_OUTPUT_ROOT_NAMES for part in parts):
            return False
        name = path.name.lower()
        return name in cls.RUNTIME_GENERATED_DIR_NAMES or path.suffix.lower() in cls.RUNTIME_GENERATED_FILE_SUFFIXES

    def _is_relative_to(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False
