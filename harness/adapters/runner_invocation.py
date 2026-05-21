from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from harness.runtime.spec import RuntimeSpec


def run_subprocess_runner(
    runner: Any,
    command: list[str],
    cwd: Path,
    timeout_seconds: float | None,
    stdout_path: Path,
    stderr_path: Path,
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    runtime_spec: RuntimeSpec | None = None,
) -> int:
    kwargs: dict[str, Any] = {"input_text": input_text, "env": env}
    if _runner_accepts_runtime_spec(runner):
        kwargs["runtime_spec"] = runtime_spec
    return runner.run(command, cwd, timeout_seconds, stdout_path, stderr_path, **kwargs)


def _runner_accepts_runtime_spec(runner: Any) -> bool:
    try:
        signature = inspect.signature(runner.run)
    except (TypeError, ValueError):
        return True
    return "runtime_spec" in signature.parameters
