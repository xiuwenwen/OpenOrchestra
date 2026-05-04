from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config/config.yaml")


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return {}
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def _minimal_yaml_load(text: str) -> dict[str, Any]:
    """Parse the small YAML subset used by config/config.yaml.

    This intentionally avoids a PyYAML dependency. It supports nested mappings
    with two-space indentation and scalar string/int/bool values.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            raise ValueError(f"Invalid config line: {raw_line!r}")
        key, value = line.split(":", 1)
        key = key.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        parsed = _parse_scalar(value)
        parent[key] = parsed
        if isinstance(parsed, dict):
            stack.append((indent, parsed))
    return root


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    return _minimal_yaml_load(config_path.read_text(encoding="utf-8"))

