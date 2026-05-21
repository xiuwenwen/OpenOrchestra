from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from harness.runtime.host import HostRuntimeExecutor
from harness.runtime.network import docker_create_network_args
from harness.runtime.spec import PathMapping, RuntimeCommandRequest, RuntimeCommandResult, RuntimeSpec, StreamCallback


class DockerRuntimeExecutor:
    mode = "docker"

    def __init__(self, host_executor: HostRuntimeExecutor | None = None, docker_binary: str = "docker"):
        self.host_executor = host_executor or HostRuntimeExecutor()
        self.docker_binary = docker_binary

    def run_capture(self, request: RuntimeCommandRequest) -> RuntimeCommandResult:
        spec = request.spec
        container_name = f"oo-runtime-{uuid.uuid4().hex[:12]}"
        create_result = self.create_container(spec, request.cwd, container_name, request)
        if create_result.returncode != 0:
            return self._failed_result(create_result, request, container_name, create_result.host_command)
        try:
            start_result = self.start_container(container_name, request)
            if start_result.returncode != 0:
                return self._failed_result(start_result, request, container_name, start_result.host_command)
            exec_result = self.exec_in_container(spec, container_name, request)
            return RuntimeCommandResult(
                returncode=exec_result.returncode,
                stdout=exec_result.stdout,
                stderr=exec_result.stderr,
                timed_out=exec_result.timed_out,
                runtime_mode=self.mode,
                image=spec.image,
                container_name=container_name,
                host_command=exec_result.host_command,
                setup_host_commands=(create_result.host_command, start_result.host_command),
                container_command=request.command,
            )
        finally:
            self.remove_container(container_name, request)

    def create_container(
        self,
        spec: RuntimeSpec,
        cwd: Path,
        container_name: str,
        request: RuntimeCommandRequest,
    ) -> RuntimeCommandResult:
        command = self._create_command(spec, cwd, container_name)
        return self._host_run(command, request)

    def start_container(self, container_name: str, request: RuntimeCommandRequest) -> RuntimeCommandResult:
        return self._host_run([self.docker_binary, "start", container_name], request)

    def exec_in_container(
        self,
        spec: RuntimeSpec,
        container_name: str,
        request: RuntimeCommandRequest,
    ) -> RuntimeCommandResult:
        command, env_file = self._exec_command(spec, container_name, request)
        try:
            return self._host_run(command, request)
        finally:
            if env_file is not None:
                env_file.unlink(missing_ok=True)

    def remove_container(self, container_name: str, request: RuntimeCommandRequest) -> RuntimeCommandResult:
        return self._host_run([self.docker_binary, "rm", "-f", container_name], request, timeout_seconds=30)

    def run_to_files(
        self,
        request: RuntimeCommandRequest,
        stdout_path: Path,
        stderr_path: Path,
        *,
        live_path: Path | None = None,
        stream_callback: StreamCallback | None = None,
    ) -> RuntimeCommandResult:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        result = self.run_capture(request)
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        if live_path is not None:
            live_path.parent.mkdir(parents=True, exist_ok=True)
            with live_path.open("a", encoding="utf-8") as live_handle:
                if result.stdout:
                    live_handle.write(f"[stdout] {result.stdout}")
                if result.stderr:
                    live_handle.write(f"[stderr] {result.stderr}")
        if stream_callback is not None:
            if result.stdout:
                stream_callback("stdout", result.stdout)
            if result.stderr:
                stream_callback("stderr", result.stderr)
        return result

    def _host_run(
        self,
        command: list[str],
        request: RuntimeCommandRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> RuntimeCommandResult:
        result = self.host_executor.run_capture(
            RuntimeCommandRequest(
                command=tuple(command),
                cwd=request.cwd,
                timeout_seconds=request.timeout_seconds if timeout_seconds is None else timeout_seconds,
                input_text=request.input_text if command[:3] == [self.docker_binary, "exec", "-i"] else None,
                env=None,
                spec=RuntimeSpec(),
            )
        )
        return RuntimeCommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=getattr(result, "timed_out", False),
            runtime_mode="host",
            host_command=tuple(command),
        )

    def _create_command(self, spec: RuntimeSpec, cwd: Path, container_name: str) -> list[str]:
        image = spec.image or "python:3.11-bookworm"
        command = [
            self.docker_binary,
            "create",
            "--name",
            container_name,
            "--workdir",
            spec.workdir,
            "-v",
            PathMapping(cwd, spec.workdir).docker_volume_arg(),
        ]
        for mount in spec.mounts:
            command.extend(["-v", mount.docker_volume_arg()])
        if spec.cache_root is not None:
            spec.cache_root.expanduser().mkdir(parents=True, exist_ok=True)
            command.extend(["-v", PathMapping(spec.cache_root.expanduser(), "/cache").docker_volume_arg()])
        command.extend(docker_create_network_args(spec.network))
        if spec.user:
            command.extend(["--user", spec.user])
        command.extend([image, "sleep", "3600"])
        return command

    def _exec_command(self, spec: RuntimeSpec, container_name: str, request: RuntimeCommandRequest) -> tuple[list[str], Path | None]:
        command = [self.docker_binary, "exec"]
        if request.input_text is not None:
            command.append("-i")
        env_file = self._write_exec_env_file(container_name, self._exec_env(spec, request))
        if env_file is not None:
            command.extend(["--env-file", str(env_file)])
        command.append(container_name)
        command.extend(request.command)
        return command, env_file

    def _exec_env(self, spec: RuntimeSpec, request: RuntimeCommandRequest) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in spec.env_allowlist:
            if key in os.environ:
                env[key] = os.environ[key]
        env.update(dict(request.env or {}))
        return env

    def _write_exec_env_file(self, container_name: str, env: dict[str, str]) -> Path | None:
        if not env:
            return None
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix=f"{container_name}-env-",
            suffix=".env",
            delete=False,
        )
        path = Path(handle.name)
        try:
            os.chmod(path, 0o600)
            for key, value in env.items():
                if not key.isidentifier() or "\n" in value or "\r" in value:
                    raise ValueError(f"Invalid Docker runtime environment variable: {key}")
                handle.write(f"{key}={value}\n")
        except Exception:
            handle.close()
            path.unlink(missing_ok=True)
            raise
        handle.close()
        return path

    def _failed_result(
        self,
        result: RuntimeCommandResult,
        request: RuntimeCommandRequest,
        container_name: str,
        host_command: list[str] | tuple[str, ...],
    ) -> RuntimeCommandResult:
        return RuntimeCommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            runtime_mode=self.mode,
            image=request.spec.image,
            container_name=container_name,
            host_command=tuple(host_command),
            container_command=request.command,
        )
