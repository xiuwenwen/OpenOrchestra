from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.adapters.process_registry import kill_process_tree, register_process, supports_process_groups, unregister_process
from harness.runtime.spec import RuntimeCommandRequest, RuntimeExecutor, RuntimeSpec


@dataclass(frozen=True)
class CapturedCommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CommandRunner:
    def __init__(self, runtime_executor: RuntimeExecutor | None = None):
        self.runtime_executor = runtime_executor

    def run_capture(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: float | None = None,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        runtime_spec: RuntimeSpec | None = None,
    ) -> CapturedCommandResult:
        self.validate_command(command)
        runtime_executor = self.runtime_executor
        if runtime_executor is None and runtime_spec is not None and runtime_spec.is_docker:
            from harness.runtime.docker import DockerRuntimeExecutor

            runtime_executor = DockerRuntimeExecutor()
        if runtime_executor is not None:
            result = runtime_executor.run_capture(
                RuntimeCommandRequest(
                    command=tuple(command),
                    cwd=cwd,
                    timeout_seconds=timeout_seconds,
                    input_text=input_text,
                    env=env,
                    spec=runtime_spec or RuntimeSpec(),
                )
            )
            return CapturedCommandResult(
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                timed_out=result.timed_out,
            )
        timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=supports_process_groups(),
            env={**os.environ, **env} if env else None,
        )
        register_process(process)
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
            return CapturedCommandResult(
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired as exc:
            kill_process_tree(process)
            stdout, stderr = process.communicate()
            stdout = self.decode_timeout_stream(stdout or exc.stdout)
            stderr = self.decode_timeout_stream(stderr or exc.stderr)
            if stderr:
                stderr += "\n"
            stderr += f"Command timed out after {exc.timeout}s."
            return CapturedCommandResult(
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )
        except KeyboardInterrupt:
            kill_process_tree(process)
            raise
        finally:
            unregister_process(process)

    def validate_command(self, command: list[str]) -> None:
        if not isinstance(command, list) or not command:
            raise ValueError("Command must be a non-empty argv list")
        if not all(isinstance(part, str) and part for part in command):
            raise ValueError("Command argv entries must be non-empty strings")

    def decode_timeout_stream(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value
