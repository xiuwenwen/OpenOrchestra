from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


COMMAND_LINE_PATTERN = re.compile(
    r"^(?:python3?|\.\/|npm|pnpm|yarn|bun|uv|streamlit|flask|fastapi|uvicorn|node|deno|go|cargo|java|mvn|gradle|make|docker|docker-compose|bash|sh)\b"
)


@dataclass(frozen=True)
class DeliveryHandoff:
    project_dir: Path
    run_command: str | None
    dependency_install: str | None


def build_delivery_handoff(result_path: Path, usage_guide: Path | None = None) -> DeliveryHandoff:
    delivery_dir = result_path.parent
    project_dir = delivery_dir / "source" if (delivery_dir / "source").is_dir() else delivery_dir
    dependency_script = project_dir / "install_dependencies.sh"
    dependency_file = next(
        (path for path in (project_dir / "requirements.txt", project_dir / "request.txt") if path.exists()),
        None,
    )
    dependency_install = None
    if dependency_script.exists():
        dependency_install = f"cd {shlex.quote(str(project_dir))} && bash install_dependencies.sh"
    elif dependency_file:
        dependency_install = f"cd {shlex.quote(str(project_dir))} && {_venv_dependency_install_command(dependency_file.name)}"
    run_command = _delivery_run_command_for_environment(project_dir, result_path, usage_guide, dependency_script.exists())
    if dependency_script.exists() and run_command:
        run_command = _use_project_virtualenv_python(run_command)
    return DeliveryHandoff(project_dir=project_dir, run_command=run_command, dependency_install=dependency_install)


def format_delivery_handoff(result_path: Path, usage_guide: Path | None = None) -> list[str]:
    handoff = build_delivery_handoff(result_path, usage_guide)
    return [
        f"project_dir: {handoff.project_dir}",
        f"run_command: {handoff.run_command or 'not found in delivery docs'}",
        f"dependency_install: {handoff.dependency_install or 'none'}",
    ]


def format_total_elapsed(task: dict[str, Any] | None) -> str:
    elapsed = _task_elapsed_seconds(task)
    return f"total_elapsed: {_format_duration(elapsed)}" if elapsed is not None else "total_elapsed: unknown"


def _task_elapsed_seconds(task: dict[str, Any] | None) -> float | None:
    if not task:
        return None
    try:
        created_at = datetime.fromisoformat(str(task["created_at"]))
        updated_at = datetime.fromisoformat(str(task["updated_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return max(0.0, (updated_at - created_at).total_seconds())


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _delivery_run_command_for_environment(
    project_dir: Path,
    result_path: Path,
    usage_guide: Path | None,
    has_dependency_installer: bool,
) -> str | None:
    for command in _delivery_run_commands(project_dir, result_path, usage_guide):
        if has_dependency_installer:
            command = _use_project_virtualenv_python(command)
        else:
            command = _normalize_delivery_python_command(command)
        if _delivery_command_is_executable(project_dir, command, has_dependency_installer):
            return command
    return _infer_executable_delivery_command(project_dir, has_dependency_installer)


def _delivery_run_commands(project_dir: Path, result_path: Path, usage_guide: Path | None) -> list[str]:
    delivery_dir = result_path.parent
    candidates = [
        usage_guide,
        delivery_dir / "usage_guide.md",
        project_dir / "README.md",
        project_dir / "readme.md",
        result_path,
    ]
    commands: list[str] = []
    for path in candidates:
        if not path or not path.exists() or not path.is_file():
            continue
        for command in _extract_shell_commands(path.read_text(encoding="utf-8", errors="replace")):
            if _is_dependency_install_command(command):
                continue
            commands.append(_with_project_cd(project_dir, command))
    return list(dict.fromkeys(commands))


def _infer_executable_delivery_command(project_dir: Path, has_dependency_installer: bool) -> str | None:
    if (project_dir / "package.json").exists():
        package = _read_json_file(project_dir / "package.json")
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        for script in ("dev", "start", "test"):
            if script in scripts and shutil.which("npm"):
                return _with_project_cd(project_dir, f"npm run {script}")
    python_bin = ".venv/bin/python" if has_dependency_installer else _available_python_command()
    if python_bin:
        for filename in ("app.py", "main.py"):
            if (project_dir / filename).exists():
                return _with_project_cd(project_dir, f"{python_bin} {filename}")
        if (project_dir / "tests").exists():
            return _with_project_cd(project_dir, f"{python_bin} -m pytest tests/")
    if (project_dir / "index.html").exists():
        return f"open {shlex.quote(str(project_dir / 'index.html'))}"
    return None


def _delivery_command_is_executable(project_dir: Path, command: str, has_dependency_installer: bool) -> bool:
    local_command = _strip_project_cd(command)
    try:
        parts = shlex.split(local_command)
    except ValueError:
        return False
    if not parts:
        return False
    executable = parts[0]
    if executable in {"python", "python3", ".venv/bin/python"}:
        if executable == ".venv/bin/python" and not has_dependency_installer and not (project_dir / executable).exists():
            return False
        if executable in {"python", "python3"} and shutil.which(executable) is None:
            return False
        return _python_command_target_exists(project_dir, parts)
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        return shutil.which(executable) is not None and (project_dir / "package.json").exists()
    if executable in {"node", "deno"}:
        return shutil.which(executable) is not None and len(parts) > 1 and (project_dir / parts[1]).exists()
    if executable in {"open"}:
        return len(parts) > 1 and Path(parts[1]).exists()
    if executable.startswith("./"):
        return os.access(project_dir / executable[2:], os.X_OK)
    return shutil.which(executable) is not None


def _python_command_target_exists(project_dir: Path, parts: list[str]) -> bool:
    if len(parts) >= 3 and parts[1] == "-m":
        module = parts[2]
        if module == "pytest":
            return (project_dir / "tests").exists() or any(project_dir.glob("test_*.py"))
        return True
    if len(parts) >= 2 and parts[1].endswith(".py"):
        return (project_dir / parts[1]).exists()
    return True


def _strip_project_cd(command: str) -> str:
    if " && " not in command:
        return command
    prefix, rest = command.split(" && ", 1)
    if prefix.startswith("cd "):
        return rest
    return command


def _available_python_command() -> str | None:
    for candidate in ("python3", "python"):
        if shutil.which(candidate):
            return candidate
    return None


def _venv_dependency_install_command(dependency_file_name: str) -> str:
    return (
        'PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python)}" && '
        '(test -n "$PYTHON_BIN" || { echo "python3 or python is required" >&2; exit 127; }) && '
        '"$PYTHON_BIN" -m venv .venv && '
        'VENV_PYTHON="${VENV_PYTHON:-.venv/bin/python}" && '
        f'"$VENV_PYTHON" -m pip install -r {shlex.quote(dependency_file_name)}'
    )


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _extract_shell_commands(text: str) -> list[str]:
    commands: list[str] = []
    fenced_blocks = re.findall(r"```(?:bash|sh|shell|zsh|console|text)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    scan_texts = fenced_blocks or [text]
    for block in scan_texts:
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "$", ">")):
                line = line.lstrip("$> ").strip()
            if not line or line.startswith("#") or line.startswith("cd "):
                continue
            if COMMAND_LINE_PATTERN.match(line):
                commands.append(line)
    return commands


def _is_dependency_install_command(command: str) -> bool:
    lowered = command.lower()
    return "pip install" in lowered or "npm install" in lowered or "pnpm install" in lowered or "yarn install" in lowered


def _with_project_cd(project_dir: Path, command: str) -> str:
    if command.startswith("cd "):
        return command
    return f"cd {shlex.quote(str(project_dir))} && {command}"


def _use_project_virtualenv_python(command: str) -> str:
    return re.sub(r"(^|&& )python3?(?=\s)", r"\1.venv/bin/python", command, count=1)


def _normalize_delivery_python_command(command: str) -> str:
    if not re.search(r"(^|&& )python(?=\s)", command):
        return command
    python_bin = _available_python_command()
    if not python_bin or python_bin == "python":
        return command
    return re.sub(r"(^|&& )python(?=\s)", rf"\1{python_bin}", command, count=1)
