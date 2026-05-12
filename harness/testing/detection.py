from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProjectProfile:
    project_type: str
    image: str
    setup_commands: tuple[str, ...] = ()
    dockerfile: str = ""


def detect_project_profile(repo_dir: Path | None, config: dict[str, Any]) -> ProjectProfile:
    docker_config = _docker_config(config)
    if repo_dir is None:
        return ProjectProfile("unknown", str(docker_config.get("default_python_image") or "python:3.11-bookworm"))
    dockerfile = repo_dir / "Dockerfile"
    if dockerfile.exists() and docker_config.get("allow_project_dockerfile", True):
        return ProjectProfile("dockerfile", "project-dockerfile", dockerfile=str(dockerfile))
    if (repo_dir / "package.json").exists():
        return ProjectProfile("node", str(docker_config.get("default_node_image") or "node:20-bookworm"))
    if _has_python_project_files(repo_dir):
        return ProjectProfile(
            "python",
            _python_image(repo_dir, docker_config),
            _python_setup_commands(repo_dir, config),
        )
    if (repo_dir / "go.mod").exists():
        return ProjectProfile("go", str(docker_config.get("default_go_image") or "golang:1.22-bookworm"))
    if (repo_dir / "Cargo.toml").exists():
        return ProjectProfile("rust", str(docker_config.get("default_rust_image") or "rust:1.78-bookworm"))
    return ProjectProfile("unknown", str(docker_config.get("default_python_image") or "python:3.11-bookworm"))


def _docker_config(config: dict[str, Any]) -> dict[str, Any]:
    testing = config.get("testing", {})
    if not isinstance(testing, dict):
        return {}
    docker = testing.get("docker", {})
    return docker if isinstance(docker, dict) else {}


def _has_python_project_files(repo_dir: Path) -> bool:
    if any((repo_dir / name).exists() for name in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "tox.ini")):
        return True
    return any(path.suffix == ".py" for path in repo_dir.rglob("*.py") if not _ignored_path(path))


def _python_image(repo_dir: Path, docker_config: dict[str, Any]) -> str:
    explicit = docker_config.get("python_image")
    if explicit:
        return str(explicit)
    version = _python_version(repo_dir)
    if version:
        return f"python:{version}-bookworm"
    return str(docker_config.get("default_python_image") or "python:3.11-bookworm")


def _python_version(repo_dir: Path) -> str | None:
    py_version = repo_dir / ".python-version"
    if py_version.exists():
        version = _normalize_python_version(py_version.read_text(encoding="utf-8", errors="replace").strip())
        if version:
            return version
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"requires-python\s*=\s*[\"']([^\"']+)[\"']", text)
        if match:
            version = _version_from_requires_python(match.group(1))
            if version:
                return version
    setup_cfg = repo_dir / "setup.cfg"
    if setup_cfg.exists():
        parser = configparser.ConfigParser()
        try:
            parser.read(setup_cfg, encoding="utf-8")
        except configparser.Error:
            parser = configparser.ConfigParser()
        if parser.has_option("options", "python_requires"):
            version = _version_from_requires_python(parser.get("options", "python_requires"))
            if version:
                return version
    return None


def _normalize_python_version(value: str) -> str | None:
    match = re.search(r"(\d+\.\d+)", value)
    return match.group(1) if match else None


def _version_from_requires_python(spec: str) -> str | None:
    supported = ["3.14", "3.13", "3.12", "3.11", "3.10", "3.9", "3.8", "3.7"]
    for version in supported:
        if _version_satisfies_spec(version, spec):
            return version
    return None


def _version_satisfies_spec(version: str, spec: str) -> bool:
    constraints = re.findall(r"(>=|<=|==|>|<|~=)\s*(3\.\d+)", spec)
    if not constraints:
        return True
    version_tuple = tuple(int(part) for part in version.split("."))
    for operator, target in constraints:
        target_tuple = tuple(int(part) for part in target.split("."))
        if operator == ">=" and version_tuple < target_tuple:
            return False
        if operator == ">" and version_tuple <= target_tuple:
            return False
        if operator == "<=" and version_tuple > target_tuple:
            return False
        if operator == "<" and version_tuple >= target_tuple:
            return False
        if operator == "==" and version_tuple != target_tuple:
            return False
        if operator == "~=" and version_tuple < target_tuple:
            return False
    return True


def _python_setup_commands(repo_dir: Path, config: dict[str, Any]) -> tuple[str, ...]:
    testing = config.get("testing", {})
    if isinstance(testing, dict):
        configured = testing.get("setup_commands")
        if isinstance(configured, list):
            return tuple(str(command) for command in configured if str(command).strip())
        docker = testing.get("docker", {})
        auto_setup = bool(docker.get("auto_setup", False)) if isinstance(docker, dict) else False
    else:
        auto_setup = False
    if not auto_setup:
        return ()
    commands = ["python -m pip install -U pip setuptools wheel"]
    if (repo_dir / "requirements.txt").exists():
        commands.append("python -m pip install -r requirements.txt")
    if any((repo_dir / name).exists() for name in ("pyproject.toml", "setup.py", "setup.cfg")):
        commands.append("python -m pip install -e .")
    if _has_pytest_tests(repo_dir):
        commands.append("python -m pip install pytest")
    return tuple(commands)


def _has_pytest_tests(repo_dir: Path) -> bool:
    for path in repo_dir.rglob("*.py"):
        if _ignored_path(path):
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py") or "tests" in path.parts:
            return True
    return False


def _ignored_path(path: Path) -> bool:
    ignored_parts = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", ".tox", ".nox"}
    return any(part in ignored_parts for part in path.parts)
