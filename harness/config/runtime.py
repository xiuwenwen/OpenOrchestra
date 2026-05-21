from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from harness.runtime.network import normalize_docker_network

from harness.config.loader import write_config_atomic
from harness.config.user_env import USER_ENV_PATH, load_user_env, write_user_env
from harness.state.repository import StateRepository


CONFIGURABLE_ROLES = ("planner", "executor", "tester", "reviewer", "communicator")
CONFIGURABLE_BACKENDS = ("codex", "claude", "gemini", "qwen")
ROLE_COUNT_ENV_KEYS = {
    "planner": "OO_PLANNER_COUNT",
    "executor": "OO_EXECUTOR_COUNT",
    "tester": "OO_TESTER_COUNT",
    "reviewer": "OO_REVIEWER_COUNT",
    "communicator": "OO_COMMUNICATOR_COUNT",
}
RUNTIME_ENV_KEYS = {
    "mode": "OO_RUNTIME",
    "image": "OO_RUNTIME_DOCKER_IMAGE",
    "network": "OO_RUNTIME_DOCKER_NETWORK",
}
MAX_ROLE_COUNT = 10


class RuntimeConfigService:
    def __init__(
        self,
        config: dict[str, Any],
        repository: StateRepository | None = None,
        config_path: str | Path | None = None,
        user_env_path: str | Path | None = USER_ENV_PATH,
    ):
        self.config = config
        self.repository = repository
        self.config_path = Path(config_path) if config_path else None
        self.user_env_path = Path(user_env_path) if user_env_path else None

    def role_runtime_config(self) -> dict[str, Any]:
        return {
            "agent_backend": dict(self.config.get("agent_backend", {})),
            "roles": {
                role: {"count": self.role_count(None, role)}
                for role in CONFIGURABLE_ROLES
                if role in self.config.get("roles", {})
            },
            "runtime": copy.deepcopy(self.config.get("runtime", {"mode": "docker"})),
            "backend_options": list(CONFIGURABLE_BACKENDS),
            "max_role_count": MAX_ROLE_COUNT,
            "scope": "runtime",
            "persist_supported": self.config_path is not None,
            "config_path": str(self.config_path) if self.config_path else None,
        }

    def apply_role_runtime_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_role_runtime_payload(payload)
        self.config.setdefault("agent_backend", {}).update(normalized["agent_backend"])
        roles = self.config.setdefault("roles", {})
        for role, role_config in normalized["roles"].items():
            roles.setdefault(role, {})["count"] = role_config["count"]
        if normalized["runtime"]:
            self._deep_update(self.config.setdefault("runtime", {}), normalized["runtime"])
        if bool(payload.get("persist")):
            if not self.config_path:
                raise ValueError("persist=true requires a config_path")
            write_config_atomic(self.config, self.config_path)
            self._persist_env_overlay_values(normalized)
        return self.role_runtime_config()

    def config_for_task(self, task_id: str | None) -> dict[str, Any]:
        merged = copy.deepcopy(self.config)
        if not task_id or not self.repository:
            return merged
        task = self.repository.get_task(task_id)
        if not task or not task.get("configuration"):
            return merged
        try:
            task_config = json.loads(task["configuration"])
        except (TypeError, json.JSONDecodeError):
            return merged
        if isinstance(task_config, dict):
            self._deep_update(merged, task_config)
        return merged

    def backend_for(self, task_id: str | None, role: str) -> str:
        config = self.config_for_task(task_id)
        agent_backend = config.get("agent_backend", {})
        return str(agent_backend.get(role) or agent_backend.get("default", "mock"))

    def role_count(self, task_id: str | None, role: str) -> int:
        config = self.config_for_task(task_id)
        return int(config.get("roles", {}).get(role, {}).get("count", 1))

    def timeout_for(self, task_id: str | None, role: str) -> int:
        config = self.config_for_task(task_id)
        return int(config.get("timeouts", {}).get(role, 0))

    def _normalize_role_runtime_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        roles_payload = payload.get("roles", {})
        backend_payload = payload.get("agent_backend", {})
        if not isinstance(roles_payload, dict):
            raise ValueError("roles must be an object")
        if not isinstance(backend_payload, dict):
            raise ValueError("agent_backend must be an object")

        roles: dict[str, dict[str, int]] = {}
        for role, role_config in roles_payload.items():
            if role not in CONFIGURABLE_ROLES:
                raise ValueError(f"Unknown role: {role}")
            if not isinstance(role_config, dict):
                raise ValueError(f"roles.{role} must be an object")
            count = int(role_config.get("count", 1))
            if count < 1 or count > MAX_ROLE_COUNT:
                raise ValueError(f"roles.{role}.count must be between 1 and {MAX_ROLE_COUNT}")
            roles[role] = {"count": count}

        agent_backend: dict[str, str] = {}
        for role, backend in backend_payload.items():
            if role not in (*CONFIGURABLE_ROLES, "default"):
                raise ValueError(f"Unknown backend target: {role}")
            backend_value = str(backend)
            if backend_value not in CONFIGURABLE_BACKENDS:
                raise ValueError(f"Unsupported backend: {backend_value}")
            agent_backend[role] = backend_value

        runtime = self._normalize_runtime_payload(payload.get("runtime", {}))
        return {"roles": roles, "agent_backend": agent_backend, "runtime": runtime}

    def _normalize_runtime_payload(self, payload: Any) -> dict[str, Any]:
        if payload in (None, {}):
            return {}
        if not isinstance(payload, dict):
            raise ValueError("runtime must be an object")
        normalized: dict[str, Any] = {}
        mode = payload.get("mode")
        if mode is not None:
            mode_value = str(mode)
            if mode_value not in {"host", "docker", "auto"}:
                raise ValueError("runtime.mode must be host, docker, or auto")
            normalized["mode"] = mode_value
        docker = payload.get("docker")
        if docker is not None:
            if not isinstance(docker, dict):
                raise ValueError("runtime.docker must be an object")
            docker_normalized: dict[str, Any] = {}
            if "image" in docker:
                docker_normalized["image"] = str(docker["image"])
            if "network" in docker:
                docker_normalized["network"] = normalize_docker_network(
                    docker["network"],
                    field="runtime.docker.network",
                    default="bridge",
                )
            if docker_normalized:
                normalized["docker"] = docker_normalized
        return normalized

    def _deep_update(self, target: dict[str, Any], updates: dict[str, Any]) -> None:
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_update(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    def _persist_env_overlay_values(self, normalized: dict[str, Any]) -> None:
        if not self.user_env_path:
            return
        values = load_user_env(self.user_env_path)
        for role, role_config in normalized["roles"].items():
            env_key = ROLE_COUNT_ENV_KEYS.get(role)
            if env_key:
                values[env_key] = str(role_config["count"])
        default_backend = normalized["agent_backend"].get("default")
        if default_backend:
            values["OO_BACKEND"] = default_backend
        runtime = normalized.get("runtime") or {}
        if runtime.get("mode"):
            values[RUNTIME_ENV_KEYS["mode"]] = str(runtime["mode"])
        docker = runtime.get("docker") if isinstance(runtime.get("docker"), dict) else {}
        if docker.get("image"):
            values[RUNTIME_ENV_KEYS["image"]] = str(docker["image"])
        if docker.get("network"):
            values[RUNTIME_ENV_KEYS["network"]] = str(docker["network"])
        write_user_env(values, self.user_env_path)
