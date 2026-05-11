from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ARTIFACT_METADATA_FILENAME = ".oo_artifact_metadata.json"


def load_artifact_metadata(directory: Path) -> dict[str, Any]:
    path = directory / ARTIFACT_METADATA_FILENAME
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_artifact_metadata(directory: Path, artifacts: dict[str, dict[str, Any]]) -> Path:
    path = directory / ARTIFACT_METADATA_FILENAME
    path.write_text(
        json.dumps({"metadata_version": 1, "artifacts": artifacts}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def artifact_metadata_entry(metadata: dict[str, Any], artifact_name: str) -> dict[str, Any]:
    artifacts = metadata.get("artifacts")
    if not isinstance(artifacts, dict):
        return {}
    entry = artifacts.get(artifact_name)
    return entry if isinstance(entry, dict) else {}


def metadata_int_field(metadata: dict[str, Any], artifact_name: str, field_name: str) -> int | None:
    value = artifact_metadata_entry(metadata, artifact_name).get(field_name)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
