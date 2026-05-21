from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config/config.yaml")


def _strip_inline_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(line):
        if char == "\\" and in_double and not escaped:
            escaped = True
            continue
        if char == "'" and not in_double and not escaped:
            in_single = not in_single
        elif char == '"' and not in_single and not escaped:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
        escaped = False
    return line.rstrip()


def _parse_scalar(value: str, *, line_no: int | None = None) -> Any:
    value = value.strip()
    if not value:
        return {}
    if value in {"|", ">"}:
        location = f" at line {line_no}" if line_no is not None else ""
        raise ValueError(f"Unsupported config value{location}: multiline YAML scalars are not supported")
    if value == "[]":
        return []
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return parsed
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

    This intentionally avoids a PyYAML dependency and is not a general YAML
    parser. It supports nested mappings, scalar string/int/bool values, scalar
    lists, list-of-mapping entries, and inline comments outside quoted strings.
    """
    root: dict[str, Any] = {}
    entries = [
        (line_no, cleaned)
        for line_no, raw_line in enumerate(text.splitlines(), start=1)
        if (cleaned := _strip_inline_comment(raw_line)).strip()
    ]
    stack: list[tuple[int, dict[str, Any] | list[Any]]] = [(-1, root)]
    for index, (line_no, raw_line) in enumerate(entries):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise ValueError(f"Invalid config indentation at line {line_no}: tabs are not supported")
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if line.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"Invalid config list item at line {line_no}: {raw_line!r}")
            item = line[2:].strip()
            if ":" in item and not item.startswith(("'", '"')):
                key, value = item.split(":", 1)
                parsed_item: dict[str, Any] = {key.strip(): _parse_scalar(value, line_no=line_no)}
                parent.append(parsed_item)
                stack.append((indent, parsed_item))
            else:
                parent.append(_parse_scalar(item, line_no=line_no))
            continue
        if ":" not in line:
            raise ValueError(f"Invalid config line at line {line_no}: {raw_line!r}")
        key, value = line.split(":", 1)
        key = key.strip()
        if not isinstance(parent, dict):
            raise ValueError(f"Invalid config mapping entry inside list at line {line_no}: {raw_line!r}")
        if not value.strip():
            parsed: dict[str, Any] | list[Any]
            parsed = [] if _next_entry_is_list(entries, index, indent) else {}
        else:
            parsed = _parse_scalar(value, line_no=line_no)
        parent[key] = parsed
        if isinstance(parsed, (dict, list)):
            stack.append((indent, parsed))
    return root


def _next_entry_is_list(entries: list[tuple[int, str]], current_index: int, current_indent: int) -> bool:
    for _, raw_line in entries[current_index + 1 :]:
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent <= current_indent:
            return False
        return raw_line.strip().startswith("- ")
    return False


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    try:
        return _minimal_yaml_load(config_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"Invalid config file {config_path}: {exc}") from exc


def dump_config(config: dict[str, Any]) -> str:
    return "\n".join(_dump_mapping(config, 0)) + "\n"


def write_config_atomic(config: dict[str, Any], path: str | Path) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config_path.with_name(f".{config_path.name}.tmp")
    temp_path.write_text(dump_config(config), encoding="utf-8")
    os.replace(temp_path, config_path)


def _dump_mapping(mapping: dict[str, Any], indent: int) -> list[str]:
    lines: list[str] = []
    prefix = " " * indent
    for key, value in mapping.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            lines.extend(_dump_mapping(value, indent + 2))
        elif isinstance(value, list):
            if not value:
                lines.append(f"{prefix}{key}: []")
            else:
                lines.append(f"{prefix}{key}:")
                item_prefix = " " * (indent + 2)
                for item in value:
                    lines.extend(_dump_list_item(item, item_prefix))
        else:
            lines.append(f"{prefix}{key}: {_dump_scalar(value)}")
    return lines


def _dump_list_item(item: Any, prefix: str) -> list[str]:
    if not isinstance(item, dict):
        return [f"{prefix}- {_dump_scalar(item)}"]
    if not item:
        return [f"{prefix}- {{}}"]
    first_key, first_value = next(iter(item.items()))
    lines = [f"{prefix}- {first_key}: {_dump_scalar(first_value)}"]
    nested_prefix = prefix + "  "
    for key, value in list(item.items())[1:]:
        if isinstance(value, dict):
            lines.append(f"{nested_prefix}{key}:")
            lines.extend(_dump_mapping(value, len(nested_prefix) + 2))
        elif isinstance(value, list):
            lines.append(f"{nested_prefix}{key}:")
            for nested_item in value:
                lines.extend(_dump_list_item(nested_item, nested_prefix + "  "))
        else:
            lines.append(f"{nested_prefix}{key}: {_dump_scalar(value)}")
    return lines


def _dump_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return '""'
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
