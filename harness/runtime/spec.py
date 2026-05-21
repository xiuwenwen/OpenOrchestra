from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol


StreamCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class PathMapping:
    host_path: Path
    container_path: str
    read_only: bool = False

    def docker_volume_arg(self) -> str:
        mode = "ro" if self.read_only else "rw"
        return f"{self.host_path.resolve()}:{self.container_path}:{mode}"


@dataclass(frozen=True)
class RuntimeSpec:
    mode: str = "host"
    image: str = ""
    workdir: str = "/workspace"
    network: str = "none"
    user: str = ""
    cache_root: Path | None = None
    env_allowlist: tuple[str, ...] = ()
    mounts: tuple[PathMapping, ...] = ()

    @property
    def is_docker(self) -> bool:
        return self.mode == "docker"


@dataclass(frozen=True)
class RuntimeCommandRequest:
    command: tuple[str, ...]
    cwd: Path
    timeout_seconds: float | None = None
    input_text: str | None = None
    env: Mapping[str, str] | None = None
    spec: RuntimeSpec = field(default_factory=RuntimeSpec)


@dataclass(frozen=True)
class RuntimeCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    runtime_mode: str = "host"
    image: str = ""
    container_name: str = ""
    host_command: tuple[str, ...] = ()
    setup_host_commands: tuple[tuple[str, ...], ...] = ()
    container_command: tuple[str, ...] = ()


class RuntimeExecutor(Protocol):
    def run_capture(self, request: RuntimeCommandRequest) -> RuntimeCommandResult:
        ...

    def run_to_files(
        self,
        request: RuntimeCommandRequest,
        stdout_path: Path,
        stderr_path: Path,
        *,
        live_path: Path | None = None,
        stream_callback: StreamCallback | None = None,
    ) -> RuntimeCommandResult:
        ...
