from __future__ import annotations

import shlex
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from harness.adapters.command_runner import CommandRunner
from harness.runtime.network import docker_create_network_args
from harness.runtime.resolver import RuntimeResolver


BACKEND_CLI_COMMANDS = {"claude": "claude", "codex": "codex", "gemini": "gemini", "qwen": "qwen"}
BACKEND_DEFAULT_API_HOSTS = {
    "claude": "api.anthropic.com",
    "codex": "api.openai.com",
    "gemini": "generativelanguage.googleapis.com",
    "qwen": "dashscope.aliyuncs.com",
}
DOCKER_DAEMON_ERROR_PATTERNS = ("cannot connect to the docker daemon", "is the docker daemon running", "error during connect")


@dataclass(frozen=True)
class RuntimePreflightResult:
    ok: bool
    message: str = ""


def check_role_runtime_preflight(
    config: dict[str, Any],
    backend: str,
    *,
    cwd: Path | None = None,
    command_runner: CommandRunner | None = None,
) -> RuntimePreflightResult:
    spec = RuntimeResolver(config).resolve()
    if not spec.is_docker or backend == "mock":
        return RuntimePreflightResult(ok=True)
    runner = command_runner or CommandRunner()
    cwd = cwd or Path.cwd()
    bootstrap = _bootstrap_config(config)
    messages: list[str] = []
    try:
        image_result = runner.run_capture(
            ["docker", "image", "inspect", spec.image],
            cwd=cwd,
            timeout_seconds=20,
        )
    except FileNotFoundError:
        return RuntimePreflightResult(False, "[ERROR] Docker binary not found. Install Docker or run with --runtime host.")
    if image_result.returncode != 0:
        detail = _first_detail(image_result.stderr, image_result.stdout)
        if _looks_like_docker_daemon_error(detail):
            return RuntimePreflightResult(False, f"[ERROR] Docker daemon is not available. Start Docker Desktop and retry. Detail: {detail}")
        if _auto_build_enabled(bootstrap):
            build_result = _build_runtime_image(runner, cwd, spec.image, bootstrap)
            if not build_result.ok:
                return build_result
            messages.append(build_result.message)
        else:
            return RuntimePreflightResult(
                ok=False,
                message=(
                    f"[ERROR] Role runtime Docker image not found: {spec.image}\n"
                    "Build it before running agent roles:\n"
                    f"  {_bundled_build_command(spec.image, bootstrap)}\n"
                    "Or choose another image with --runtime-docker-image.\n"
                    f"Detail: {detail}"
                ),
            )

    cli_command = BACKEND_CLI_COMMANDS.get(backend)
    if not cli_command or not _require_backend_cli(bootstrap):
        return RuntimePreflightResult(ok=True, message="\n".join(item for item in messages if item))
    user_result = _check_non_root_runtime(runner, cwd, spec.image)
    if user_result.returncode != 0 and _auto_build_enabled(bootstrap) and not messages:
        build_result = _build_runtime_image(runner, cwd, spec.image, bootstrap)
        if not build_result.ok:
            return build_result
        messages.append(build_result.message)
        user_result = _check_non_root_runtime(runner, cwd, spec.image)
    if user_result.returncode != 0:
        return RuntimePreflightResult(
            False,
            f"[ERROR] Role runtime Docker image still runs as root: {spec.image}\n"
            f"Rebuild with a non-root runtime user:\n  {_bundled_build_command(spec.image, bootstrap)}",
        )
    cli_result = _check_backend_cli(runner, cwd, spec.image, cli_command)
    if cli_result.returncode != 0 and _auto_build_enabled(bootstrap) and not messages:
        build_result = _build_runtime_image(runner, cwd, spec.image, bootstrap)
        if not build_result.ok:
            return build_result
        messages.append(build_result.message)
        cli_result = _check_backend_cli(runner, cwd, spec.image, cli_command)
    if cli_result.returncode == 0:
        api_host = _backend_api_host(backend)
        if api_host:
            network_result = _check_backend_api_connectivity(runner, cwd, spec.image, spec.network, api_host)
            if network_result.returncode != 0:
                detail = _first_detail(network_result.stderr, network_result.stdout)
                return RuntimePreflightResult(
                    False,
                    (
                        f"[ERROR] Role runtime Docker network cannot reach backend API host: {api_host}\n"
                        f"Image: {spec.image}\n"
                        f"Network: {spec.network}\n"
                        "Use a role runtime network with outbound API access, for example runtime.docker.network=bridge.\n"
                        f"Detail: {detail}"
                    ),
                )
        return RuntimePreflightResult(ok=True, message="\n".join(item for item in messages if item))
    detail = _first_detail(cli_result.stderr, cli_result.stdout)
    if _looks_like_docker_daemon_error(detail):
        return RuntimePreflightResult(False, f"[ERROR] Docker daemon is not available. Start Docker Desktop and retry. Detail: {detail}")
    return RuntimePreflightResult(
        ok=False,
        message=(
            f"[ERROR] Role runtime Docker image is missing backend CLI: {cli_command}\n"
            f"Image: {spec.image}\n"
            "Rebuild the bundled image with agent CLIs enabled:\n"
            f"  {_bundled_build_command(spec.image, bootstrap)}\n"
            "Or choose another image containing the selected backend CLI.\n"
            f"Detail: {detail}"
        ),
    )


def _build_runtime_image(runner: CommandRunner, cwd: Path, image: str, bootstrap: dict[str, Any]) -> RuntimePreflightResult:
    context = Path(str(bootstrap.get("build_context") or "docker/agent-runtime")).expanduser()
    if not context.is_absolute():
        context = cwd / context
    if not context.exists():
        return RuntimePreflightResult(
            ok=False,
            message=(
                f"[ERROR] Runtime Docker build context not found: {context}\n"
                f"Expected context for image: {image}"
            ),
        )
    install_clis = "true" if bool(bootstrap.get("install_agent_clis", True)) else "false"
    command = ["docker", "build", "--build-arg", f"INSTALL_AGENT_CLIS={install_clis}", "-t", image, str(context)]
    result = runner.run_capture(command, cwd=cwd, timeout_seconds=float(bootstrap.get("build_timeout_seconds") or 1800))
    if result.returncode == 0:
        return RuntimePreflightResult(True, f"[runtime] built role runtime image: {image}")
    detail = _first_detail(result.stderr, result.stdout)
    return RuntimePreflightResult(False, f"[ERROR] Failed to build role runtime image: {image}\nCommand: {' '.join(command)}\nDetail: {detail}")


def _check_backend_cli(runner: CommandRunner, cwd: Path, image: str, cli_command: str):
    return runner.run_capture(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--entrypoint",
            "sh",
            image,
            "-lc",
            f"command -v {shlex.quote(cli_command)} >/dev/null",
        ],
        cwd=cwd,
        timeout_seconds=30,
    )


def _check_backend_api_connectivity(runner: CommandRunner, cwd: Path, image: str, network: str, api_host: str):
    script = (
        "import socket, sys\n"
        "host = sys.argv[1]\n"
        "socket.create_connection((host, 443), timeout=5).close()\n"
    )
    return runner.run_capture(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            *docker_create_network_args(network),
            "--entrypoint",
            "python3",
            image,
            "-c",
            script,
            api_host,
        ],
        cwd=cwd,
        timeout_seconds=15,
    )


def _check_non_root_runtime(runner: CommandRunner, cwd: Path, image: str):
    return runner.run_capture(
        ["docker", "run", "--rm", "--pull=never", "--network", "none", "--entrypoint", "sh", image, "-lc", 'test "$(id -u)" != "0"'],
        cwd=cwd,
        timeout_seconds=30,
    )


def _bootstrap_config(config: dict[str, Any]) -> dict[str, Any]:
    runtime = config.get("runtime", {}) if isinstance(config, dict) else {}
    docker = runtime.get("docker", {}) if isinstance(runtime, dict) else {}
    bootstrap = docker.get("bootstrap", {}) if isinstance(docker, dict) else {}
    return bootstrap if isinstance(bootstrap, dict) else {}


def _auto_build_enabled(bootstrap: dict[str, Any]) -> bool:
    return bool(bootstrap.get("enabled", True)) and bool(bootstrap.get("auto_build", True))


def _require_backend_cli(bootstrap: dict[str, Any]) -> bool:
    return bool(bootstrap.get("require_backend_cli", True))


def _bundled_build_command(image: str, bootstrap: dict[str, Any] | None = None) -> str:
    bootstrap = bootstrap or {}
    install_clis = "true" if bool(bootstrap.get("install_agent_clis", True)) else "false"
    context = shlex.quote(str(bootstrap.get("build_context") or "docker/agent-runtime"))
    return f"docker build --build-arg INSTALL_AGENT_CLIS={install_clis} -t {shlex.quote(image)} {context}"


def _first_detail(stderr: str, stdout: str) -> str:
    text = (stderr or stdout or "no details").strip()
    text = " ".join(text.split())
    return text[:500]


def _looks_like_docker_daemon_error(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in DOCKER_DAEMON_ERROR_PATTERNS)


def _backend_api_host(backend: str) -> str:
    if backend == "claude":
        configured = _first_configured_url_host(
            (
                Path.home() / ".claude" / "settings.json",
                Path.home() / ".claude.json",
            )
        )
        if configured:
            return configured
    return BACKEND_DEFAULT_API_HOSTS.get(backend, "")


def _first_configured_url_host(paths: tuple[Path, ...]) -> str:
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        for value in _walk_json_values(payload):
            if not isinstance(value, str) or "://" not in value:
                continue
            parsed = urlparse(value)
            if parsed.hostname:
                return parsed.hostname
    return ""


def _walk_json_values(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_json_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_values(item)
    else:
        yield value
