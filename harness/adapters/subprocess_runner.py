from __future__ import annotations

import os
import json
import subprocess
import sys
import threading
from pathlib import Path

from harness.adapters.command_runner import CommandRunner
from harness.adapters.process_registry import kill_process_tree, register_process, supports_process_groups, unregister_process
from harness.runtime.spec import RuntimeCommandRequest, RuntimeExecutor, RuntimeSpec
from harness.ui.terminal import TerminalStatusLine


class SubprocessRunner:
    def __init__(
        self,
        stream_output: bool = False,
        stream_prefix: str = "",
        runtime_executor: RuntimeExecutor | None = None,
    ):
        self.stream_output = stream_output
        self.stream_prefix = stream_prefix
        self.command_runner = CommandRunner()
        self.runtime_executor = runtime_executor

    def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: float | None,
        stdout_path: Path,
        stderr_path: Path,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        runtime_spec: RuntimeSpec | None = None,
    ) -> int:
        self.command_runner.validate_command(command)
        runtime_executor = self.runtime_executor
        if runtime_executor is None and runtime_spec is not None and runtime_spec.is_docker:
            from harness.runtime.docker import DockerRuntimeExecutor

            runtime_executor = DockerRuntimeExecutor()
        if runtime_executor is not None:
            return self._run_with_runtime_executor(
                command,
                cwd,
                timeout_seconds,
                stdout_path,
                stderr_path,
                input_text=input_text,
                env=env,
                runtime_spec=runtime_spec,
                runtime_executor=runtime_executor,
            )
        timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_handle, (stdout_path.parent / "live.log").open("a", encoding="utf-8") as live_handle:
                live_lock = threading.Lock()
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.PIPE if input_text is not None else None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    start_new_session=supports_process_groups(),
                    env={**os.environ, **env} if env else None,
                )
                register_process(process)
                stdout_thread = threading.Thread(
                    target=self._copy_stream,
                    args=(process.stdout, stdout_handle, sys.stdout, live_handle, live_lock, "stdout"),
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=self._copy_stream,
                    args=(process.stderr, stderr_handle, sys.stderr, live_handle, live_lock, "stderr"),
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
                    kill_process_tree(process)
                    return_code = 124
                    stderr_handle.write("\nTIMEOUT\n")
                    stderr_handle.flush()
                    with live_lock:
                        live_handle.write("\n[harness] TIMEOUT\n")
                        live_handle.flush()
                except KeyboardInterrupt:
                    kill_process_tree(process)
                    raise
                finally:
                    unregister_process(process)
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)
                return return_code
        except subprocess.TimeoutExpired as exc:
            # Kept for compatibility with tests or alternate subprocess implementations.
            stdout_path.write_text(self.command_runner.decode_timeout_stream(exc.stdout), encoding="utf-8")
            stderr_path.write_text(self.command_runner.decode_timeout_stream(exc.stderr) + "\nTIMEOUT\n", encoding="utf-8")
            return 124

    def _run_with_runtime_executor(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: float | None,
        stdout_path: Path,
        stderr_path: Path,
        *,
        input_text: str | None,
        env: dict[str, str] | None,
        runtime_spec: RuntimeSpec | None,
        runtime_executor: RuntimeExecutor,
    ) -> int:
        def stream_callback(stream_name: str, chunk: str) -> None:
            if not self.stream_output:
                return
            terminal_handle = sys.stderr if stream_name == "stderr" else sys.stdout
            TerminalStatusLine.write_live(f"{self.stream_prefix}{chunk}", terminal_handle)

        result = runtime_executor.run_to_files(
            RuntimeCommandRequest(
                command=tuple(command),
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                input_text=input_text,
                env=env,
                spec=runtime_spec or RuntimeSpec(),
            ),
            stdout_path,
            stderr_path,
            live_path=stdout_path.parent / "live.log",
            stream_callback=stream_callback,
        )
        self._write_runtime_invocation(stdout_path.parent, result)
        return result.returncode

    def _write_runtime_invocation(self, log_dir: Path, result) -> None:
        payload = {
            "schema_version": "runtime_invocation.v1",
            "runtime_mode": result.runtime_mode,
            "image": result.image,
            "container_name": result.container_name,
            "setup_host_commands": [list(command) for command in result.setup_host_commands],
            "host_command": list(result.host_command),
            "container_command": list(result.container_command),
            "returncode": result.returncode,
            "timed_out": result.timed_out,
        }
        (log_dir / "runtime_invocation.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _copy_stream(self, stream, handle, terminal_handle, live_handle, live_lock: threading.Lock, stream_name: str) -> None:
        if stream is None:
            return
        try:
            for chunk in iter(stream.readline, ""):
                if not chunk:
                    break
                handle.write(chunk)
                handle.flush()
                with live_lock:
                    live_handle.write(f"[{stream_name}] {chunk}")
                    live_handle.flush()
                if self.stream_output:
                    TerminalStatusLine.write_live(f"{self.stream_prefix}{chunk}", terminal_handle)
        finally:
            stream.close()
