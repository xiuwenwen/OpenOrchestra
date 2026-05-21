from __future__ import annotations

from harness.core.errors import TaskFailedError


LEGACY_NETWORK_POLICIES = {"install_only", "always"}


def normalize_docker_network(value: object, *, field: str, default: str) -> str:
    raw = str(value if value is not None else default).strip()
    if not raw:
        raw = default
    lowered = raw.lower()
    if lowered in LEGACY_NETWORK_POLICIES:
        raise TaskFailedError(
            f"{field}={raw!r} is no longer supported. "
            "Use explicit Docker networks: bridge, none, default, host, or a custom network name."
        )
    if lowered in {"none", "bridge", "default", "host"}:
        return lowered
    return raw


def docker_create_network_args(network: str) -> list[str]:
    normalized = normalize_docker_network(network, field="docker.network", default="none")
    if normalized == "default":
        return []
    return ["--network", normalized]


def docker_network_name(network: str) -> str:
    normalized = normalize_docker_network(network, field="docker.network", default="none")
    if normalized == "default":
        return "bridge"
    return normalized
