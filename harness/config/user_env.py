from __future__ import annotations

from pathlib import Path
from typing import Any


USER_ENV_PATH = Path.home() / ".openorchestra.env"
LEGACY_USER_ENV_PATH = Path.home() / ".myharness.env"
ROLE_COUNT_ENV_KEYS = {
    "OO_PLANNER_COUNT": "planner",
    "OO_EXECUTOR_COUNT": "executor",
    "OO_TESTER_COUNT": "tester",
    "OO_REVIEWER_COUNT": "reviewer",
    "OO_JUDGE_COUNT": "judge",
    "OO_COMMUNICATOR_COUNT": "communicator",
}
ENV_CONFIG_SPECS: dict[str, tuple[tuple[str, ...], type]] = {
    "OO_BACKEND": (("agent_backend", "default"), str),
    "OO_WORKSPACE_ROOT": (("system", "workspace_root"), str),
    "OO_ARTIFACT_ROOT": (("system", "artifact_root"), str),
    "OO_DELIVER_ROOT": (("system", "deliver_root"), str),
    "OO_STATE_DB": (("system", "state_db"), str),
    "OO_SOURCE_REPO": (("system", "source_repo"), str),
    "OO_PLANNER_COUNT": (("roles", "planner", "count"), int),
    "OO_EXECUTOR_COUNT": (("roles", "executor", "count"), int),
    "OO_TESTER_COUNT": (("roles", "tester", "count"), int),
    "OO_REVIEWER_COUNT": (("roles", "reviewer", "count"), int),
    "OO_JUDGE_COUNT": (("roles", "judge", "count"), int),
    "OO_COMMUNICATOR_COUNT": (("roles", "communicator", "count"), int),
    "OO_MAX_PLANNING_ROUNDS": (("limits", "max_planning_rounds"), int),
    "OO_PLANNING_PEER_REVIEW_LOOPS": (("limits", "planning_peer_review_loops"), int),
    "OO_MAX_TEST_FIX_ROUNDS": (("limits", "max_test_fix_rounds"), str),
    "OO_MAX_REVIEW_ROUNDS": (("limits", "max_review_rounds"), int),
    "OO_MAX_AGENT_RETRY": (("limits", "max_agent_retry"), int),
    "OO_TIMEOUT_PLANNER": (("timeouts", "planner"), int),
    "OO_TIMEOUT_EXECUTOR": (("timeouts", "executor"), int),
    "OO_TIMEOUT_TESTER": (("timeouts", "tester"), int),
    "OO_TIMEOUT_REVIEWER": (("timeouts", "reviewer"), int),
    "OO_TIMEOUT_JUDGE": (("timeouts", "judge"), int),
    "OO_TIMEOUT_COMMUNICATOR": (("timeouts", "communicator"), int),
    "OO_HEARTBEAT_INTERVAL_SECONDS": (("heartbeat", "interval_seconds"), int),
    "OO_UI_HOST": (("visualization", "host"), str),
    "OO_UI_PORT": (("visualization", "port"), int),
    "OO_CLAUDE_MAX_TOKENS_CLASSIFIER": (("claude", "max_output_tokens", "classifier"), int),
    "OO_CLAUDE_MAX_TOKENS_MISC": (("claude", "max_output_tokens", "misc"), int),
    "OO_CLAUDE_MAX_TOKENS_PLANNER": (("claude", "max_output_tokens", "planner"), int),
    "OO_CLAUDE_MAX_TOKENS_EXECUTOR": (("claude", "max_output_tokens", "executor"), int),
    "OO_CLAUDE_MAX_TOKENS_TESTER": (("claude", "max_output_tokens", "tester"), int),
    "OO_CLAUDE_MAX_TOKENS_REVIEWER": (("claude", "max_output_tokens", "reviewer"), int),
    "OO_CLAUDE_MAX_TOKENS_JUDGE": (("claude", "max_output_tokens", "judge"), int),
    "OO_CLAUDE_MAX_TOKENS_COMMUNICATOR": (("claude", "max_output_tokens", "communicator"), int),
    "OO_CLAUDE_CONTEXT_WINDOW_TOKENS": (("claude", "context_window_tokens"), int),
    "OO_CLAUDE_CONTEXT_WINDOW_BUFFER_TOKENS": (("claude", "context_window_buffer_tokens"), int),
    "OO_ARTIFACT_INPUT_MAX_FILES": (("artifact_input", "max_files"), int),
    "OO_ARTIFACT_INPUT_MAX_FILE_BYTES": (("artifact_input", "max_file_bytes"), int),
    "OO_ARTIFACT_INPUT_MAX_TOTAL_BYTES": (("artifact_input", "max_total_bytes"), int),
    "OO_POLICY_DIFFERENT_ROLES_CAN_RUN_CONCURRENTLY": (("policy", "different_roles_can_run_concurrently"), bool),
    "OO_POLICY_SAME_ROLE_CAN_RUN_CONCURRENTLY": (("policy", "same_role_can_run_concurrently"), bool),
    "OO_POLICY_ALLOW_MEDIUM_BUG_DELIVERY": (("policy", "allow_medium_bug_delivery"), bool),
    "OO_POLICY_REQUIRE_ALL_TESTS_PASS": (("policy", "require_all_tests_pass"), bool),
}
LEGACY_ENV_ALIASES = {key.replace("OO_", "HARNESS_", 1): key for key in ENV_CONFIG_SPECS}


def load_user_env(path: Path = USER_ENV_PATH) -> dict[str, str]:
    if path == USER_ENV_PATH:
        values = _read_user_env_file(LEGACY_USER_ENV_PATH)
        values.update(_read_user_env_file(USER_ENV_PATH))
        return canonicalize_user_env(values)
    return canonicalize_user_env(_read_user_env_file(path))


def _read_user_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def canonicalize_user_env(values: dict[str, str]) -> dict[str, str]:
    canonical: dict[str, str] = {}
    for key, value in values.items():
        canonical[LEGACY_ENV_ALIASES.get(key, key)] = value
    return canonical


def save_user_env_value(key: str, value: str, path: Path = USER_ENV_PATH) -> None:
    values = load_user_env(path)
    values[key] = value
    write_user_env(values, path)


def write_user_env(values: dict[str, str], path: Path = USER_ENV_PATH) -> None:
    lines = ["# OpenOrchestra persistent CLI settings"]
    lines.extend(f"{name}={values[name]}" for name in sorted(values))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_user_env_defaults(config: dict[str, Any], values: dict[str, str], path: Path = USER_ENV_PATH) -> None:
    updated = dict(values)
    for key, (config_path, _value_type) in ENV_CONFIG_SPECS.items():
        if key not in updated:
            value = get_nested_config(config, config_path)
            if value is not None:
                updated[key] = str(value).lower() if isinstance(value, bool) else str(value)
    if updated != values:
        write_user_env(updated, path)


def apply_env_role_counts(config: dict[str, Any], values: dict[str, str]) -> None:
    for key, role in ROLE_COUNT_ENV_KEYS.items():
        if key not in values:
            continue
        try:
            count = int(values[key])
        except ValueError:
            continue
        if count > 0:
            config["roles"][role]["count"] = count


def apply_user_env_config(config: dict[str, Any], values: dict[str, str]) -> None:
    for key, (config_path, value_type) in ENV_CONFIG_SPECS.items():
        if key not in values:
            continue
        try:
            value = parse_env_value(values[key], value_type)
        except ValueError:
            continue
        if value_type is int and int(value) < 0:
            continue
        set_nested_config(config, config_path, value)


def parse_env_value(raw_value: str, value_type: type) -> Any:
    if value_type is bool:
        lowered = raw_value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Invalid boolean value: {raw_value}")
    if value_type is int:
        return int(raw_value)
    return raw_value


def get_nested_config(config: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def set_nested_config(config: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = config
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value
