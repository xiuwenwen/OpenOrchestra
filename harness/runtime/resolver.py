from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.errors import TaskFailedError
from harness.runtime.network import normalize_docker_network
from harness.runtime.spec import RuntimeSpec


class RuntimeResolver:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def resolve(self, context: str = "agent") -> RuntimeSpec:
        runtime = self.config.get("runtime", {})
        runtime = runtime if isinstance(runtime, dict) else {}
        mode = str(runtime.get("mode") or "docker").strip().lower()
        if mode == "auto":
            mode = "docker" if bool(self._docker_config().get("enabled", False)) else "host"
        if mode == "host":
            return RuntimeSpec(mode="host")
        if mode != "docker":
            raise TaskFailedError(f"Invalid runtime.mode: {mode!r}")
        docker = self._docker_config()
        image = str(docker.get("image") or docker.get("default_image") or "openorchestra-agent-runtime:latest")
        workdir = str(docker.get("workdir") or "/workspace")
        network = self._network_for_context(context, docker)
        user = str(docker.get("user") or "")
        cache_root = docker.get("cache_root")
        env_allowlist = docker.get("env_allowlist") or []
        if not isinstance(env_allowlist, list):
            raise TaskFailedError("runtime.docker.env_allowlist must be a list")
        return RuntimeSpec(
            mode="docker",
            image=image,
            workdir=workdir,
            network=network,
            user=user,
            cache_root=Path(str(cache_root)).expanduser() if cache_root else None,
            env_allowlist=tuple(str(item) for item in env_allowlist if str(item).strip()),
        )

    def _network_for_context(self, context: str, docker: dict[str, Any]) -> str:
        normalized_context = str(context or "agent").strip().lower()
        if normalized_context == "agent":
            return normalize_docker_network(
                docker.get("network"),
                field="runtime.docker.network",
                default="bridge",
            )
        if normalized_context == "patch_gate":
            patch_gate = self.config.get("patch_gate", {})
            patch_docker = patch_gate.get("docker", {}) if isinstance(patch_gate, dict) else {}
            return normalize_docker_network(
                patch_docker.get("network") if isinstance(patch_docker, dict) else None,
                field="patch_gate.docker.network",
                default="none",
            )
        if normalized_context == "final_validation":
            final_validation = self.config.get("final_validation", {})
            final_docker = final_validation.get("docker", {}) if isinstance(final_validation, dict) else {}
            return normalize_docker_network(
                final_docker.get("network") if isinstance(final_docker, dict) else None,
                field="final_validation.docker.network",
                default="none",
            )
        raise TaskFailedError(f"Invalid runtime resolver context: {context!r}")

    def _docker_config(self) -> dict[str, Any]:
        runtime = self.config.get("runtime", {})
        runtime = runtime if isinstance(runtime, dict) else {}
        docker = runtime.get("docker", {})
        return docker if isinstance(docker, dict) else {}
