from __future__ import annotations

from pathlib import Path


def list_output_files(output_dir: Path) -> list[Path]:
    return sorted(path for path in output_dir.rglob("*") if path.is_file())

