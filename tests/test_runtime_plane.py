from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from harness.adapters.command_runner import CapturedCommandResult, CommandRunner
from harness.adapters.subprocess_runner import SubprocessRunner
from harness.agents.runner import AgentPhaseRunner
from harness.core.errors import TaskFailedError
from harness.runtime import DockerRuntimeExecutor, HostRuntimeExecutor, RuntimeResolver
from harness.runtime.preflight import check_role_runtime_preflight
from harness.runtime.spec import RuntimeCommandRequest, RuntimeCommandResult, RuntimeSpec


class FakeCaptureRunner:
    def __init__(self, *results: CapturedCommandResult):
        self.commands: list[list[str]] = []
        self.results = list(results)

    def run_capture(self, command: list[str], **kwargs) -> CapturedCommandResult:
        self.commands.append(command)
        return self.results.pop(0)


def test_runtime_resolver_defaults_to_docker() -> None:
    spec = RuntimeResolver({}).resolve()

    assert spec.mode == "docker"
    assert spec.is_docker
    assert spec.image == "openorchestra-agent-runtime:latest"


def test_runtime_resolver_supports_explicit_host() -> None:
    spec = RuntimeResolver({"runtime": {"mode": "host"}}).resolve()

    assert spec.mode == "host"
    assert not spec.is_docker


def test_runtime_resolver_reads_docker_config(tmp_path: Path) -> None:
    spec = RuntimeResolver(
        {
            "runtime": {
                "mode": "docker",
                "docker": {
                    "image": "example/runtime:1",
                    "workdir": "/workspace",
                    "network": "bridge",
                    "cache_root": str(tmp_path / "cache"),
                    "env_allowlist": ["TOKEN_A", "TOKEN_B"],
                },
            }
        }
    ).resolve()

    assert spec.mode == "docker"
    assert spec.image == "example/runtime:1"
    assert spec.workdir == "/workspace"
    assert spec.network == "bridge"
    assert spec.cache_root == tmp_path / "cache"
    assert spec.env_allowlist == ("TOKEN_A", "TOKEN_B")


def test_runtime_resolver_rejects_invalid_runtime_mode() -> None:
    with pytest.raises(TaskFailedError, match="Invalid runtime.mode"):
        RuntimeResolver({"runtime": {"mode": "vm"}}).resolve()


def test_runtime_resolver_rejects_legacy_network_policy() -> None:
    with pytest.raises(TaskFailedError, match="install_only"):
        RuntimeResolver({"runtime": {"mode": "docker", "docker": {"network": "install_only"}}}).resolve()


def test_runtime_resolver_uses_boundary_specific_gate_networks() -> None:
    config = {
        "runtime": {"mode": "docker", "docker": {"image": "agent:test", "network": "bridge"}},
        "patch_gate": {"docker": {"network": "none"}},
        "final_validation": {"docker": {"network": "default"}},
    }

    resolver = RuntimeResolver(config)

    assert resolver.resolve().network == "bridge"
    assert resolver.resolve(context="patch_gate").network == "none"
    assert resolver.resolve(context="final_validation").network == "default"


def test_role_runtime_preflight_skips_host_runtime(tmp_path: Path) -> None:
    runner = FakeCaptureRunner()
    result = check_role_runtime_preflight(
        {"runtime": {"mode": "host"}},
        "claude",
        cwd=tmp_path,
        command_runner=runner,
    )

    assert result.ok
    assert runner.commands == []


def test_role_runtime_preflight_reports_missing_docker_image(tmp_path: Path) -> None:
    runner = FakeCaptureRunner(CapturedCommandResult(1, "", "Error response from daemon: No such image: missing:latest"))
    result = check_role_runtime_preflight(
        {
            "runtime": {
                "mode": "docker",
                "docker": {"image": "missing:latest", "bootstrap": {"auto_build": False}},
            }
        },
        "claude",
        cwd=tmp_path,
        command_runner=runner,
    )

    assert not result.ok
    assert runner.commands == [["docker", "image", "inspect", "missing:latest"]]
    assert "Role runtime Docker image not found: missing:latest" in result.message
    assert "docker build --build-arg INSTALL_AGENT_CLIS=true -t missing:latest docker/agent-runtime" in result.message


def test_role_runtime_preflight_auto_builds_missing_image_then_checks_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build_context = tmp_path / "docker" / "agent-runtime"
    build_context.mkdir(parents=True)
    monkeypatch.setattr("harness.runtime.preflight._backend_api_host", lambda backend: "api.example.test")
    runner = FakeCaptureRunner(
        CapturedCommandResult(1, "", "Error response from daemon: No such image: agent:test"),
        CapturedCommandResult(0, "built", ""),
        CapturedCommandResult(0, "", ""),
        CapturedCommandResult(0, "/usr/local/bin/claude\n", ""),
        CapturedCommandResult(0, "", ""),
    )

    result = check_role_runtime_preflight(
        {
            "runtime": {
                "mode": "docker",
                "docker": {
                    "image": "agent:test",
                    "bootstrap": {"build_context": str(build_context), "install_agent_clis": True},
                },
            }
        },
        "claude",
        cwd=tmp_path,
        command_runner=runner,
    )

    assert result.ok
    assert result.message == "[runtime] built role runtime image: agent:test"
    assert runner.commands[1][:5] == ["docker", "build", "--build-arg", "INSTALL_AGENT_CLIS=true", "-t"]
    assert runner.commands[1][-2:] == ["agent:test", str(build_context)]
    assert runner.commands[2][-3:] == ["agent:test", "-lc", 'test "$(id -u)" != "0"']
    assert runner.commands[3][-3:] == ["agent:test", "-lc", "command -v claude >/dev/null"]
    assert "--network" in runner.commands[4] and "bridge" in runner.commands[4]
    assert runner.commands[4][-1] == "api.example.test"


def test_role_runtime_preflight_rebuilds_existing_root_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build_context = tmp_path / "docker" / "agent-runtime"
    build_context.mkdir(parents=True)
    monkeypatch.setattr("harness.runtime.preflight._backend_api_host", lambda backend: "api.example.test")
    runner = FakeCaptureRunner(
        CapturedCommandResult(0, "[]", ""),
        CapturedCommandResult(1, "", ""),
        CapturedCommandResult(0, "built", ""),
        CapturedCommandResult(0, "", ""),
        CapturedCommandResult(0, "/usr/local/bin/claude\n", ""),
        CapturedCommandResult(0, "", ""),
    )

    result = check_role_runtime_preflight(
        {
            "runtime": {
                "mode": "docker",
                "docker": {
                    "image": "agent:test",
                    "bootstrap": {"build_context": str(build_context), "install_agent_clis": True},
                },
            }
        },
        "claude",
        cwd=tmp_path,
        command_runner=runner,
    )

    assert result.ok
    assert runner.commands[1][-3:] == ["agent:test", "-lc", 'test "$(id -u)" != "0"']
    assert runner.commands[2][:2] == ["docker", "build"]
    assert runner.commands[3][-3:] == ["agent:test", "-lc", 'test "$(id -u)" != "0"']
    assert runner.commands[4][-3:] == ["agent:test", "-lc", "command -v claude >/dev/null"]
    assert runner.commands[5][-1] == "api.example.test"


def test_role_runtime_preflight_reports_missing_backend_cli(tmp_path: Path) -> None:
    runner = FakeCaptureRunner(CapturedCommandResult(0, "[]", ""), CapturedCommandResult(0, "", ""), CapturedCommandResult(127, "", ""))
    result = check_role_runtime_preflight(
        {
            "runtime": {
                "mode": "docker",
                "docker": {"image": "agent:test", "bootstrap": {"auto_build": False}},
            }
        },
        "claude",
        cwd=tmp_path,
        command_runner=runner,
    )

    assert not result.ok
    assert runner.commands[0] == ["docker", "image", "inspect", "agent:test"]
    assert runner.commands[1][-3:] == ["agent:test", "-lc", 'test "$(id -u)" != "0"']
    assert runner.commands[2][-3:] == ["agent:test", "-lc", "command -v claude >/dev/null"]
    assert "Role runtime Docker image is missing backend CLI: claude" in result.message
    assert "docker build --build-arg INSTALL_AGENT_CLIS=true -t agent:test docker/agent-runtime" in result.message


def test_role_runtime_preflight_reports_backend_api_network_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.runtime.preflight._backend_api_host", lambda backend: "api.example.test")
    runner = FakeCaptureRunner(
        CapturedCommandResult(0, "[]", ""),
        CapturedCommandResult(0, "", ""),
        CapturedCommandResult(0, "/usr/local/bin/claude\n", ""),
        CapturedCommandResult(1, "", "socket.gaierror: Temporary failure in name resolution"),
    )

    result = check_role_runtime_preflight(
        {
            "runtime": {
                "mode": "docker",
                "docker": {"image": "agent:test", "network": "none", "bootstrap": {"auto_build": False}},
            }
        },
        "claude",
        cwd=tmp_path,
        command_runner=runner,
    )

    assert not result.ok
    assert "Role runtime Docker network cannot reach backend API host: api.example.test" in result.message
    assert "Network: none" in result.message


def test_agent_runtime_spec_mounts_backend_config_paths(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_json = tmp_path / ".claude.json"
    claude_dir.mkdir()
    claude_json.write_text("{}", encoding="utf-8")
    workspace = SimpleNamespace(
        workspace_dir=tmp_path / "workspace",
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        log_dir=tmp_path / "logs",
    )
    config = {
        "runtime": {
            "mode": "docker",
            "docker": {
                "image": "agent:test",
                "backend_config_mounts": {
                    "claude": [
                        {"host": str(claude_dir), "container": "/home/openorchestra/.claude", "read_only": True},
                        {"host": str(claude_json), "container": "/home/openorchestra/.claude.json", "read_only": True},
                    ]
                },
            },
        }
    }

    spec = AgentPhaseRunner(orchestrator=None).resolve_agent_runtime_spec(config, workspace, "claude")

    assert any(mount.host_path == workspace.workspace_dir and mount.container_path == "/openorchestra" for mount in spec.mounts)
    assert any(mount.host_path == claude_dir and mount.container_path == "/home/openorchestra/.claude" and mount.read_only for mount in spec.mounts)
    writable_session_env = (
        tmp_path
        / "workspace"
        / "runtime"
        / "backend-config-writable"
        / "claude"
        / "home__openorchestra__.claude"
        / "session-env"
    )
    assert any(
        mount.host_path == writable_session_env
        and mount.container_path == "/home/openorchestra/.claude/session-env"
        and not mount.read_only
        for mount in spec.mounts
    )
    assert any(mount.host_path == claude_json and mount.container_path == "/home/openorchestra/.claude.json" and mount.read_only for mount in spec.mounts)


def test_agent_runtime_spec_rejects_legacy_single_mount_object(tmp_path: Path) -> None:
    workspace = SimpleNamespace(input_dir=tmp_path / "input", output_dir=tmp_path / "output", log_dir=tmp_path / "logs")
    config = {
        "runtime": {
            "mode": "docker",
            "docker": {
                "image": "agent:test",
                "backend_config_mounts": {
                    "claude": {"host": str(tmp_path / ".claude"), "container": "/home/openorchestra/.claude"}
                },
            },
        }
    }

    with pytest.raises(TaskFailedError, match=r"backend_config_mounts\.claude must be a list"):
        AgentPhaseRunner(orchestrator=None).resolve_agent_runtime_spec(config, workspace, "claude")


def test_host_runtime_executor_captures_command(tmp_path: Path) -> None:
    result = HostRuntimeExecutor().run_capture(
        RuntimeCommandRequest(
            command=(sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"),
            cwd=tmp_path,
        )
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert result.runtime_mode == "host"


def test_command_runner_can_delegate_to_runtime_executor(tmp_path: Path) -> None:
    seen: list[RuntimeCommandRequest] = []

    class FakeExecutor:
        def run_capture(self, request: RuntimeCommandRequest) -> RuntimeCommandResult:
            seen.append(request)
            return RuntimeCommandResult(returncode=3, stdout="out", stderr="err", timed_out=True)

    result = CommandRunner(runtime_executor=FakeExecutor()).run_capture(
        ["tool", "arg"],
        cwd=tmp_path,
        timeout_seconds=9,
        input_text="prompt",
        env={"A": "B"},
    )

    assert result.returncode == 3
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.timed_out
    assert seen[0].command == ("tool", "arg")
    assert seen[0].cwd == tmp_path
    assert seen[0].timeout_seconds == 9
    assert seen[0].input_text == "prompt"
    assert seen[0].env == {"A": "B"}


def test_subprocess_runner_can_delegate_to_runtime_executor(tmp_path: Path, capsys) -> None:
    class FakeExecutor:
        def run_to_files(
            self,
            request: RuntimeCommandRequest,
            stdout_path: Path,
            stderr_path: Path,
            *,
            live_path: Path | None = None,
            stream_callback=None,
        ) -> RuntimeCommandResult:
            stdout_path.write_text("out\n", encoding="utf-8")
            stderr_path.write_text("err\n", encoding="utf-8")
            if live_path is not None:
                live_path.write_text("[stdout] out\n", encoding="utf-8")
            if stream_callback is not None:
                stream_callback("stdout", "out\n")
                stream_callback("stderr", "err\n")
            return RuntimeCommandResult(returncode=7)

    exit_code = SubprocessRunner(
        stream_output=True,
        stream_prefix="[runtime] ",
        runtime_executor=FakeExecutor(),
    ).run(
        ["tool"],
        cwd=tmp_path,
        timeout_seconds=1,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    captured = capsys.readouterr()
    assert exit_code == 7
    assert (tmp_path / "stdout.log").read_text(encoding="utf-8") == "out\n"
    assert (tmp_path / "stderr.log").read_text(encoding="utf-8") == "err\n"
    assert (tmp_path / "runtime_invocation.json").exists()
    assert "[runtime] out" in captured.out
    assert "[runtime] err" in captured.err


def test_docker_runtime_executor_builds_isolated_container_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_A", "secret")
    seen: list[tuple[str, ...]] = []

    class FakeHostExecutor:
        def run_capture(self, request: RuntimeCommandRequest) -> RuntimeCommandResult:
            assert request.env is None
            seen.append(request.command)
            if request.command[:2] == ("docker", "exec"):
                assert request.input_text == "stdin"
                return RuntimeCommandResult(returncode=0, stdout="inside\n", host_command=request.command)
            return RuntimeCommandResult(returncode=0, host_command=request.command)

    spec = RuntimeSpec(
        mode="docker",
        image="example/runtime:1",
        workdir="/workspace",
        network="bridge",
        user="1000:1000",
        cache_root=tmp_path / "cache",
        env_allowlist=("TOKEN_A",),
    )

    result = DockerRuntimeExecutor(host_executor=FakeHostExecutor()).run_capture(
        RuntimeCommandRequest(
            command=("python", "-V"),
            cwd=tmp_path,
            timeout_seconds=5,
            input_text="stdin",
            env={"EXTRA": "1"},
            spec=spec,
        )
    )

    create_command = seen[0]
    exec_command = seen[2]
    cleanup_command = seen[3]
    assert result.returncode == 0
    assert result.stdout == "inside\n"
    assert create_command[:4] == ("docker", "create", "--name", result.container_name)
    assert ("--workdir", "/workspace") == (create_command[4], create_command[5])
    assert "--network" in create_command and "bridge" in create_command
    assert "--user" in create_command and "1000:1000" in create_command
    assert f"{tmp_path.resolve()}:/workspace:rw" in create_command
    assert exec_command[:4] == ("docker", "exec", "-i", "--env-file")
    env_file = Path(exec_command[4])
    assert "TOKEN_A=secret" not in exec_command
    assert "EXTRA=1" not in exec_command
    assert exec_command[-2:] == ("python", "-V")
    assert not env_file.exists()
    assert exec_command[-2:] == ("python", "-V")
    assert cleanup_command[:3] == ("docker", "rm", "-f")
