from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CapturedCommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CommandRunner:
    def run_capture(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: float | None = None,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CapturedCommandResult:
        self.validate_command(command)
        timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env={**os.environ, **env} if env else None,
            )
            return CapturedCommandResult(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = self.decode_timeout_stream(exc.stderr)
            if stderr:
                stderr += "\n"
            stderr += f"Command timed out after {exc.timeout}s."
            return CapturedCommandResult(
                returncode=124,
                stdout=self.decode_timeout_stream(exc.stdout),
                stderr=stderr,
                timed_out=True,
            )

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
