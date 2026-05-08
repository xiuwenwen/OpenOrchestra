from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote


class HarnessFileReader:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def read_file(self, path_text: str, max_chars: int = 200_000) -> dict[str, Any]:
        path = Path(unquote(path_text)).expanduser().resolve()
        if not self.is_allowed_path(path):
            raise PermissionError(f"Path is outside Harness readable roots: {path}")
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))
        raw = path.read_text(encoding="utf-8", errors="replace")
        truncated = len(raw) > max_chars
        text = raw[-max_chars:] if truncated else raw
        return {
            "path": str(path),
            "size": path.stat().st_size,
            "text": text,
            "truncated_from_start": truncated,
        }

    def is_allowed_path(self, path: Path) -> bool:
        roots = [
            self.config["system"].get("workspace_root", "./workspaces"),
            self.config["system"].get("artifact_root", "./artifacts"),
            self.config["system"].get("deliver_root", "./deliver"),
            "logs",
        ]
        resolved_roots = [Path(str(root)).expanduser().resolve() for root in roots]
        return any(_is_relative_to(path, root) for root in resolved_roots)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
