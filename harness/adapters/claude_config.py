from __future__ import annotations

import math
import re
from typing import Any


DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000
DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS = 2_048
DEFAULT_MAX_OUTPUT_TOKENS_BY_ROLE = {
    "classifier": 2048,
    "misc": 64000,
    "planner": 64000,
    "executor": 64000,
    "tester": 64000,
    "reviewer": 64000,
    "judge": 64000,
    "communicator": 64000,
}
DEFAULT_MAX_OUTPUT_TOKENS = 16000
MIN_DYNAMIC_MAX_OUTPUT_TOKENS = 64
CJK_CHARACTER_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class ClaudeContextBudgetError(ValueError):
    def __init__(
        self,
        *,
        role: str,
        context_window: int,
        buffer_tokens: int,
        estimated_input_tokens: int,
        available_output_tokens: int,
        minimum_output_tokens: int,
    ):
        self.role = role
        self.context_window = context_window
        self.buffer_tokens = buffer_tokens
        self.estimated_input_tokens = estimated_input_tokens
        self.available_output_tokens = available_output_tokens
        self.minimum_output_tokens = minimum_output_tokens
        super().__init__(
            "Estimated Claude request exceeds the configured context budget: "
            f"role={role}, context_window={context_window}, buffer_tokens={buffer_tokens}, "
            f"estimated_input_tokens={estimated_input_tokens}, available_output_tokens={available_output_tokens}, "
            f"minimum_output_tokens={minimum_output_tokens}. Reduce staged artifacts or increase context_window_tokens."
        )


def claude_env_for_role(config: dict[str, Any] | None, role: str, prompt: str | None = None) -> dict[str, str]:
    max_output_tokens = claude_max_output_tokens(config or {}, role)
    if max_output_tokens is None:
        return {}
    if prompt is not None:
        max_output_tokens = claude_dynamic_max_output_tokens(config or {}, role, prompt, max_output_tokens)
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


def claude_dynamic_max_output_tokens(
    config: dict[str, Any],
    role: str,
    prompt: str,
    configured_max_output_tokens: int | None = None,
) -> int | None:
    max_output_tokens = configured_max_output_tokens
    if max_output_tokens is None:
        max_output_tokens = claude_max_output_tokens(config, role)
    if max_output_tokens is None:
        return None
    context_window = claude_context_window_tokens(config)
    if context_window is None:
        return max_output_tokens
    buffer_tokens = claude_context_window_buffer_tokens(config)
    estimated_input_tokens = estimate_prompt_tokens(prompt)
    available_output_tokens = context_window - buffer_tokens - estimated_input_tokens
    if available_output_tokens < MIN_DYNAMIC_MAX_OUTPUT_TOKENS:
        raise ClaudeContextBudgetError(
            role=role,
            context_window=context_window,
            buffer_tokens=buffer_tokens,
            estimated_input_tokens=estimated_input_tokens,
            available_output_tokens=available_output_tokens,
            minimum_output_tokens=MIN_DYNAMIC_MAX_OUTPUT_TOKENS,
        )
    if available_output_tokens >= max_output_tokens:
        return max_output_tokens
    return available_output_tokens


def claude_context_window_tokens(config: dict[str, Any]) -> int | None:
    claude_config = config.get("claude", {}) if isinstance(config, dict) else {}
    if not isinstance(claude_config, dict) or "context_window_tokens" not in claude_config:
        return DEFAULT_CONTEXT_WINDOW_TOKENS
    return _parse_optional_positive_tokens(claude_config["context_window_tokens"], "claude.context_window_tokens")


def claude_context_window_buffer_tokens(config: dict[str, Any]) -> int:
    claude_config = config.get("claude", {}) if isinstance(config, dict) else {}
    if not isinstance(claude_config, dict) or "context_window_buffer_tokens" not in claude_config:
        return DEFAULT_CONTEXT_WINDOW_BUFFER_TOKENS
    value = _parse_max_output_tokens(claude_config["context_window_buffer_tokens"], "claude.context_window_buffer_tokens")
    return value or 0


def estimate_prompt_tokens(prompt: str) -> int:
    cjk_count = len(CJK_CHARACTER_RE.findall(prompt))
    non_cjk_text = CJK_CHARACTER_RE.sub("", prompt)
    non_cjk_bytes = len(non_cjk_text.encode("utf-8"))
    return cjk_count + math.ceil(non_cjk_bytes / 4)


def _parse_max_output_tokens(value: Any, field_name: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer >= 0, got {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be an integer >= 0, got {parsed}")
    return parsed or None


def _parse_optional_positive_tokens(value: Any, field_name: str) -> int | None:
    parsed = _parse_max_output_tokens(value, field_name)
    if parsed is not None and parsed <= 0:
        raise ValueError(f"{field_name} must be an integer >= 0, got {parsed}")
    return parsed
