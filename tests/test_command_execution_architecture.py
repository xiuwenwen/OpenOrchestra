from __future__ import annotations

import ast
from pathlib import Path


ALLOWED_SUBPROCESS_CALL_FILES = {
    Path("harness/adapters/command_runner.py"),
    Path("harness/adapters/subprocess_runner.py"),
    Path("harness/runtime/host.py"),
}


def test_external_command_execution_is_centralized() -> None:
    offenders: list[str] = []
    for path in sorted(Path("harness").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_subprocess_execution_call(node):
                continue
            if path not in ALLOWED_SUBPROCESS_CALL_FILES:
                offenders.append(f"{path}:{node.lineno}")

    assert offenders == []


def test_shell_true_is_not_used_for_external_commands() -> None:
    offenders: list[str] = []
    for path in sorted(Path("harness").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    offenders.append(f"{path}:{node.lineno}")

    assert offenders == []


def _is_subprocess_execution_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
    )
