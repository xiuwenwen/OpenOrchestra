from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from harness.adapters.command_runner import CommandRunner
from harness.ui.terminal import TerminalStatusLine


class SubprocessRunner:
    def __init__(self, stream_output: bool = False, stream_prefix: str = ""):
        self.stream_output = stream_output
        self.stream_prefix = stream_prefix
        self.command_runner = CommandRunner()

    def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: float | None,
        stdout_path: Path,
        stderr_path: Path,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> int:
        self.command_runner.validate_command(command)
        timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_handle:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.PIPE if input_text is not None else None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    env={**os.environ, **env} if env else None,
                )
                stdout_thread = threading.Thread(
                    target=self._copy_stream,
                    args=(process.stdout, stdout_handle, sys.stdout),
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=self._copy_stream,
                    args=(process.stderr, stderr_handle, sys.stderr),
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                if input_text is not None and process.stdin is not None:
                    process.stdin.write(input_text)
                    process.stdin.close()
                try:
                    return_code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    return_code = 124
                    stderr_handle.write("\nTIMEOUT\n")
                    stderr_handle.flush()
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)
                return return_code
        except subprocess.TimeoutExpired as exc:
            # Kept for compatibility with tests or alternate subprocess implementations.
            stdout_path.write_text(self.command_runner.decode_timeout_stream(exc.stdout), encoding="utf-8")
            stderr_path.write_text(self.command_runner.decode_timeout_stream(exc.stderr) + "\nTIMEOUT\n", encoding="utf-8")
            return 124

    def _copy_stream(self, stream, handle, live_handle) -> None:
        if stream is None:
            return
        try:
            for chunk in iter(stream.readline, ""):
                if not chunk:
                    break
                handle.write(chunk)
                handle.flush()
                if self.stream_output:
                    TerminalStatusLine.write_live(f"{self.stream_prefix}{chunk}", live_handle)
        finally:
            stream.close()
