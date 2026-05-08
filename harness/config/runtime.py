from __future__ import annotations

import copy
import json
from typing import Any

from harness.state.repository import StateRepository


CONFIGURABLE_ROLES = ("planner", "executor", "tester", "reviewer", "judge", "communicator")
CONFIGURABLE_BACKENDS = ("codex", "claude", "gemini", "qwen", "mock")
MAX_ROLE_COUNT = 10


class RuntimeConfigService:
    def __init__(self, config: dict[str, Any], repository: StateRepository | None = None):
        self.config = config
        self.repository = repository

    def role_runtime_config(self) -> dict[str, Any]:
        return {
            "agent_backend": dict(self.config.get("agent_backend", {})),
            "roles": {
                role: {"count": self.role_count(None, role)}
                for role in CONFIGURABLE_ROLES
                if role in self.config.get("roles", {})
            },
            "backend_options": list(CONFIGURABLE_BACKENDS),
            "max_role_count": MAX_ROLE_COUNT,
            "scope": "runtime",
        }

    def apply_role_runtime_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_role_runtime_payload(payload)
        self.config.setdefault("agent_backend", {}).update(normalized["agent_backend"])
        roles = self.config.setdefault("roles", {})
        for role, role_config in normalized["roles"].items():
            roles.setdefault(role, {})["count"] = role_config["count"]
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

        return {"roles": roles, "agent_backend": agent_backend}

    def _deep_update(self, target: dict[str, Any], updates: dict[str, Any]) -> None:
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_update(target[key], value)
            else:
                target[key] = copy.deepcopy(value)
