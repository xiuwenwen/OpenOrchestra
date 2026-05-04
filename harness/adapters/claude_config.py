from __future__ import annotations

from typing import Any


DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE = {
    "classifier": 2048,
    "misc": 168000,
    "planner": 128000,
    "executor": 64000,
    "tester": 64000,
    "reviewer": 128000,
    "judge": 128000,
    "communicator": 64000,
}
DEFAULT_MAX_OUTPUT_TOKENS = 16000


def claude_env_for_role(config: dict[str, Any] | None, role: str) -> dict[str, str]:
    max_output_tokens = claude_max_output_tokens(config or {}, role)
    if max_output_tokens is None:
        return {}
    return {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens)}


def claude_max_output_tokens(config: dict[str, Any], role: str) -> int | None:
    claude_config = config.get("claude", {}) if isinstance(config, dict) else {}
    configured = claude_config.get("max_output_tokens") if isinstance(claude_config, dict) else None
    if isinstance(configured, dict):
        if role in configured:
            return _parse_max_output_tokens(configured[role], f"claude.max_output_tokens.{role}")
        if "default" in configured:
            return _parse_max_output_tokens(configured["default"], "claude.max_output_tokens.default")
        return DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE.get(role, DEFAULT_MAX_OUTPUT_TOKENS)
    if configured is not None:
        return _parse_max_output_tokens(configured, "claude.max_output_tokens")
    return DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE.get(role, DEFAULT_MAX_OUTPUT_TOKENS)


def _parse_max_output_tokens(value: Any, field_name: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer >= 0, got {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be an integer >= 0, got {parsed}")
    return parsed or None
