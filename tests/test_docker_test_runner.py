from __future__ import annotations

import sys
from pathlib import Path

from harness.adapters.command_runner import CapturedCommandResult
from harness.testing.detection import ProjectProfile
from harness.testing.runners import DockerTestRunner, TestRunRequest

class FakeCommandRunner:
    def __init__(self, *results: CapturedCommandResult) -> None:
        self.results = list(results)
        self.commands: list[list[str]] = []

    def run_capture(self, command, cwd, timeout_seconds=None, input_text=None, env=None):
        self.commands.append(list(command))
        return self.results.pop(0) if self.results else CapturedCommandResult(returncode=0, stdout="ok\n", stderr="")

def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo

def _request(
    tmp_path: Path,
    repo: Path,
    commands: tuple[str, ...],
    profile: ProjectProfile | None = None,
    setup_commands: tuple[str, ...] = (),
) -> TestRunRequest:
    return TestRunRequest(
        repo_dir=repo,
        commands=commands,
        setup_commands=setup_commands,
        log_dir=tmp_path / "logs",
        timeout_seconds=5,
        profile=profile or ProjectProfile("python", "python:3.11-bookworm"),
        config={"testing": {"docker": {"test_network": "none", "cache_root": str(tmp_path / "cache")}}},
    )

def test_docker_runner_executes_commands_without_shell(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.testing.runners.docker.shutil.which", lambda name: "/usr/bin/docker")
    runner = FakeCommandRunner()
    evidence = DockerTestRunner(runner).run(_request(tmp_path, _repo(tmp_path), ("python -c \"print('ok')\"; touch marker",)))
    exec_commands = [command for command in runner.commands if command[:2] == ["docker", "exec"]]
    assert evidence.status == "pass"
    assert exec_commands[0][3:5] == ["python", "-c"]
    assert "sh" not in exec_commands[0][3:5]
    assert not (tmp_path / "marker").exists()

def test_docker_runner_reports_blocked_when_docker_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.testing.runners.docker.shutil.which", lambda name: None)
    evidence = DockerTestRunner(FakeCommandRunner()).run(_request(tmp_path, _repo(tmp_path), ("python -m pytest -q",)))
    assert evidence.status == "fail"
    assert evidence.environment_status == "blocked"
    assert evidence.failure_type == "infra"

def test_docker_runner_rejects_host_path_commands_before_create(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.testing.runners.docker.shutil.which", lambda name: "/usr/bin/docker")
    runner = FakeCommandRunner()
    evidence = DockerTestRunner(runner).run(_request(tmp_path, _repo(tmp_path), (f"{sys.executable} -m pytest -q",)))
    assert evidence.failure_type == "test_command"
    assert evidence.test_status == "blocked"
    assert evidence.commands[0].scope == "container"
    assert "host path" in evidence.commands[0].stderr
    assert runner.commands == []

def test_docker_exec_126_is_environment_blocked(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.testing.runners.docker.shutil.which", lambda name: "/usr/bin/docker")
    runner = FakeCommandRunner(
        CapturedCommandResult(returncode=0, stdout="", stderr=""),
        CapturedCommandResult(returncode=0, stdout="", stderr=""),
        CapturedCommandResult(returncode=126, stdout="", stderr="command not found"),
    )
    evidence = DockerTestRunner(runner).run(_request(tmp_path, _repo(tmp_path), ("python -m pytest -q",)))
    assert evidence.failure_type == "env_setup"
    assert evidence.test_status == "blocked"
    assert evidence.commands[-1].container_command == "python -m pytest -q"
    assert evidence.commands[-1].workdir == "/workspace"

def test_docker_runner_builds_project_dockerfile_before_exec(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.testing.runners.docker.shutil.which", lambda name: "/usr/bin/docker")
    repo = _repo(tmp_path)
    dockerfile = repo / "Dockerfile"
    dockerfile.write_text("FROM python:3.11-bookworm\n", encoding="utf-8")
    runner = FakeCommandRunner()
    profile = ProjectProfile("dockerfile", "project-dockerfile", dockerfile=str(dockerfile))
    evidence = DockerTestRunner(runner).run(_request(tmp_path, repo, ("python -V",), profile))
    build_commands = [command for command in runner.commands if command[:2] == ["docker", "build"]]
    create_commands = [command for command in runner.commands if command[:2] == ["docker", "create"]]

    assert evidence.status == "pass"
    assert build_commands
    assert any(part.startswith("oo-test-image-") for part in create_commands[0])


def test_docker_runner_switches_from_setup_network_to_test_network(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.testing.runners.docker.shutil.which", lambda name: "/usr/bin/docker")
    repo = _repo(tmp_path)
    runner = FakeCommandRunner()

    evidence = DockerTestRunner(runner).run(
        _request(
            tmp_path,
            repo,
            ("python -m pytest -q",),
            setup_commands=("python -m pip install -e .",),
        )
    )

    create_command = next(command for command in runner.commands if command[:2] == ["docker", "create"])
    switch_command = next(command for command in runner.commands if command[:2] == ["sh", "-lc"])
    assert evidence.status == "pass"
    assert "--network" in create_command and "bridge" in create_command
    assert "docker network disconnect bridge" in switch_command[-1]
