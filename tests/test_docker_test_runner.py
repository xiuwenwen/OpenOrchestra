from __future__ import annotations

from pathlib import Path

from harness.adapters.command_runner import CapturedCommandResult
from harness.testing.detection import ProjectProfile
from harness.testing.runners import DockerTestRunner, TestRunRequest


class FakeCommandRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run_capture(self, command, cwd, timeout_seconds=None, input_text=None, env=None):
        self.commands.append(list(command))
        return CapturedCommandResult(returncode=0, stdout="ok\n", stderr="")


def test_docker_runner_executes_commands_without_shell(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.testing.runners.docker.shutil.which", lambda name: "/usr/bin/docker")
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = FakeCommandRunner()

    evidence = DockerTestRunner(runner).run(
        TestRunRequest(
            repo_dir=repo,
            commands=(f"python -c \"print('ok')\"; touch {tmp_path / 'marker'}",),
            setup_commands=(),
            log_dir=tmp_path / "logs",
            timeout_seconds=5,
            profile=ProjectProfile("python", "python:3.11-bookworm"),
            config={"testing": {"docker": {"network": "none", "cache_root": str(tmp_path / "cache")}}},
        )
    )

    exec_commands = [command for command in runner.commands if command[:2] == ["docker", "exec"]]
    assert evidence.status == "pass"
    assert exec_commands
    assert exec_commands[0][3:5] == ["python", "-c"]
    assert "sh" not in exec_commands[0][3:5]
    assert not (tmp_path / "marker").exists()


def test_docker_runner_reports_blocked_when_docker_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.testing.runners.docker.shutil.which", lambda name: None)
    repo = tmp_path / "repo"
    repo.mkdir()

    evidence = DockerTestRunner(FakeCommandRunner()).run(
        TestRunRequest(
            repo_dir=repo,
            commands=("python -m pytest -q",),
            setup_commands=(),
            log_dir=tmp_path / "logs",
            timeout_seconds=5,
            profile=ProjectProfile("python", "python:3.11-bookworm"),
            config={"testing": {"docker": {}}},
        )
    )

    assert evidence.status == "fail"
    assert evidence.environment_status == "blocked"
    assert evidence.failure_type == "infra"


def test_docker_runner_builds_project_dockerfile_before_exec(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.testing.runners.docker.shutil.which", lambda name: "/usr/bin/docker")
    repo = tmp_path / "repo"
    repo.mkdir()
    dockerfile = repo / "Dockerfile"
    dockerfile.write_text("FROM python:3.11-bookworm\n", encoding="utf-8")
    runner = FakeCommandRunner()

    evidence = DockerTestRunner(runner).run(
        TestRunRequest(
            repo_dir=repo,
            commands=("python -V",),
            setup_commands=(),
            log_dir=tmp_path / "logs",
            timeout_seconds=5,
            profile=ProjectProfile("dockerfile", "project-dockerfile", dockerfile=str(dockerfile)),
            config={"testing": {"docker": {"network": "none", "cache_root": str(tmp_path / "cache")}}},
        )
    )

    build_commands = [command for command in runner.commands if command[:2] == ["docker", "build"]]
    create_commands = [command for command in runner.commands if command[:2] == ["docker", "create"]]
    assert evidence.status == "pass"
    assert build_commands
    assert create_commands
    assert any(part.startswith("oo-test-image-") for part in create_commands[0])
