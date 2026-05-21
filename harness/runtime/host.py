from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from harness.adapters.process_registry import kill_process_tree, register_process, supports_process_groups, unregister_process
from harness.runtime.spec import RuntimeCommandRequest, RuntimeCommandResult, StreamCallback


class HostRuntimeExecutor:
    mode = "host"

    def run_capture(self, request: RuntimeCommandRequest) -> RuntimeCommandResult:
        timeout = request.timeout_seconds if request.timeout_seconds and request.timeout_seconds > 0 else None
        process = subprocess.Popen(
            list(request.command),
            cwd=request.cwd,
            stdin=subprocess.PIPE if request.input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=supports_process_groups(),
            env={**os.environ, **dict(request.env or {})} if request.env else None,
        )
        register_process(process)
        try:
            stdout, stderr = process.communicate(input=request.input_text, timeout=timeout)
            return RuntimeCommandResult(
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                runtime_mode=self.mode,
                host_command=request.command,
            )
        except subprocess.TimeoutExpired as exc:
            kill_process_tree(process)
            stdout, stderr = process.communicate()
            stdout = self.decode_timeout_stream(stdout or exc.stdout)
            stderr = self.decode_timeout_stream(stderr or exc.stderr)
            if stderr:
                stderr += "\n"
            stderr += f"Command timed out after {exc.timeout}s."
            return RuntimeCommandResult(
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                runtime_mode=self.mode,
                host_command=request.command,
            )
        except KeyboardInterrupt:
            kill_process_tree(process)
            raise
        finally:
            unregister_process(process)

    def run_to_files(
        self,
        request: RuntimeCommandRequest,
        stdout_path: Path,
        stderr_path: Path,
        *,
        live_path: Path | None = None,
        stream_callback: StreamCallback | None = None,
    ) -> RuntimeCommandResult:
        timeout = request.timeout_seconds if request.timeout_seconds and request.timeout_seconds > 0 else None
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        live_handle = live_path.open("a", encoding="utf-8") if live_path else None
        live_lock = threading.Lock()
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_handle:
                process = subprocess.Popen(
                    list(request.command),
                    cwd=request.cwd,
                    stdin=subprocess.PIPE if request.input_text is not None else None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    start_new_session=supports_process_groups(),
                    env={**os.environ, **dict(request.env or {})} if request.env else None,
                )
                register_process(process)
                stdout_thread = threading.Thread(
                    target=self._copy_stream,
                    args=(process.stdout, stdout_handle, live_handle, live_lock, "stdout", stream_callback),
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=self._copy_stream,
                    args=(process.stderr, stderr_handle, live_handle, live_lock, "stderr", stream_callback),
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                if request.input_text is not None and process.stdin is not None:
                    process.stdin.write(request.input_text)
                    process.stdin.close()
                try:
                    return_code = process.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    kill_process_tree(process)
                    return_code = 124
                    timed_out = True
                    stderr_handle.write("\nTIMEOUT\n")
                    stderr_handle.flush()
                    self._write_live(live_handle, live_lock, "\n[harness] TIMEOUT\n")
                except KeyboardInterrupt:
                    kill_process_tree(process)
                    raise
                finally:
                    unregister_process(process)
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)
                return RuntimeCommandResult(
                    returncode=return_code,
                    timed_out=timed_out,
                    runtime_mode=self.mode,
                    host_command=request.command,
                )
        finally:
            if live_handle is not None:
                live_handle.close()

    def _copy_stream(
        self,
        stream,
        handle,
        live_handle,
        live_lock: threading.Lock,
        stream_name: str,
        stream_callback: StreamCallback | None,
    ) -> None:
        if stream is None:
            return
        try:
            for chunk in iter(stream.readline, ""):
                if not chunk:
                    break
                handle.write(chunk)
                handle.flush()
                self._write_live(live_handle, live_lock, f"[{stream_name}] {chunk}")
                if stream_callback is not None:
                    stream_callback(stream_name, chunk)
        finally:
            stream.close()

    def _write_live(self, live_handle, live_lock: threading.Lock, text: str) -> None:
        if live_handle is None:
            return
        with live_lock:
            live_handle.write(text)
            live_handle.flush()

    def decode_timeout_stream(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value
