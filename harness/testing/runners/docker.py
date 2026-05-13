from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from harness.adapters.command_runner import CommandRunner
from harness.testing.evidence import CommandEvidence, TestRunEvidence, evidence_from_command_results
from harness.testing.runners.base import TestRunRequest, split_command


class DockerTestRunner:
    runtime = "docker"

    def __init__(self, command_runner: CommandRunner | None = None, docker_binary: str = "docker"):
        self.command_runner = command_runner or CommandRunner()
        self.docker_binary = docker_binary

    def run(self, request: TestRunRequest) -> TestRunEvidence:
        if request.repo_dir is None:
            return TestRunEvidence(
                status="fail",
                runtime=self.runtime,
                image=request.profile.image,
                project_type=request.profile.project_type,
                environment_status="fail",
                build_status="blocked",
                test_status="blocked",
                failure_type="infra",
                commands=(CommandEvidence(name="repository", command="n/a", exit_code=None, stderr="No materialized repo exists."),),
            )
        if shutil.which(self.docker_binary) is None:
            return TestRunEvidence(
                status="fail",
                runtime=self.runtime,
                image=request.profile.image,
                project_type=request.profile.project_type,
                environment_status="blocked",
                build_status="blocked",
                test_status="blocked",
                failure_type="infra",
                notes=(f"Docker binary not found on PATH: {self.docker_binary}",),
            )
        request.log_dir.mkdir(parents=True, exist_ok=True)
        container_name = f"oo-test-{uuid.uuid4().hex[:12]}"
        image = request.profile.image
        temporary_image = ""
        commands: list[CommandEvidence] = []
        created = False
        try:
            if request.profile.dockerfile:
                temporary_image = f"oo-test-image-{uuid.uuid4().hex[:12]}"
                build_evidence = self._build_project_image(request, temporary_image)
                commands.append(build_evidence)
                if build_evidence.exit_code != 0:
                    return evidence_from_command_results(
                        runtime=self.runtime,
                        image=temporary_image,
                        project_type=request.profile.project_type,
                        commands=commands,
                    )
                image = temporary_image
            create_result = self._create_container(request, container_name, image)
            create_stdout = request.log_dir / "docker_create.stdout.log"
            create_stderr = request.log_dir / "docker_create.stderr.log"
            create_stdout.write_text(create_result.stdout, encoding="utf-8")
            create_stderr.write_text(create_result.stderr, encoding="utf-8")
            if create_result.returncode != 0:
                return TestRunEvidence(
                    status="fail",
                    runtime=self.runtime,
                    image=image,
                    project_type=request.profile.project_type,
                    environment_status="fail",
                    build_status="blocked",
                    test_status="blocked",
                    failure_type="env_setup",
                    commands=(
                        CommandEvidence(
                            name="docker_create",
                            command=f"docker create {request.profile.image}",
                            exit_code=create_result.returncode,
                            stdout=str(create_stdout),
                            stderr=str(create_stderr),
                            phase="setup",
                        ),
                    ),
                )
            created = True
            start_result = self.command_runner.run_capture(
                [self.docker_binary, "start", container_name],
                cwd=request.repo_dir,
                timeout_seconds=request.timeout_seconds,
            )
            if start_result.returncode != 0:
                start_stdout = request.log_dir / "docker_start.stdout.log"
                start_stderr = request.log_dir / "docker_start.stderr.log"
                start_stdout.write_text(start_result.stdout, encoding="utf-8")
                start_stderr.write_text(start_result.stderr, encoding="utf-8")
                return TestRunEvidence(
                    status="fail",
                    runtime=self.runtime,
                    image=image,
                    project_type=request.profile.project_type,
                    environment_status="fail",
                    build_status="blocked",
                    test_status="blocked",
                    failure_type="env_setup",
                    commands=(
                        CommandEvidence(
                            name="docker_start",
                            command=f"docker start {container_name}",
                            exit_code=start_result.returncode,
                            stdout=str(start_stdout),
                            stderr=str(start_stderr),
                            phase="setup",
                        ),
                    ),
                )
            commands.extend(self._exec_commands(request, container_name, request.setup_commands, phase="setup"))
            if any(command.exit_code != 0 for command in commands):
                return evidence_from_command_results(
                    runtime=self.runtime,
                    image=image,
                    project_type=request.profile.project_type,
                    commands=commands,
                )
            commands.extend(self._exec_commands(request, container_name, request.commands, phase="test"))
            return evidence_from_command_results(
                runtime=self.runtime,
                image=image,
                project_type=request.profile.project_type,
                commands=commands,
            )
        finally:
            if created:
                self.command_runner.run_capture(
                    [self.docker_binary, "rm", "-f", container_name],
                    cwd=request.repo_dir,
                    timeout_seconds=30,
                )
            if temporary_image:
                self.command_runner.run_capture(
                    [self.docker_binary, "rmi", "-f", temporary_image],
                    cwd=request.repo_dir,
                    timeout_seconds=30,
                )

    def _build_project_image(self, request: TestRunRequest, image: str) -> CommandEvidence:
        stdout_path = request.log_dir / "docker_build.stdout.log"
        stderr_path = request.log_dir / "docker_build.stderr.log"
        command = [
            self.docker_binary,
            "build",
            "-f",
            request.profile.dockerfile,
            "-t",
            image,
        ]
        network = self._network_for_phase(request, setup_phase=True)
        if network != "default":
            command.extend(["--network", network])
        command.append(str(request.repo_dir.resolve()))
        completed = self.command_runner.run_capture(command, cwd=request.repo_dir, timeout_seconds=request.timeout_seconds)
        exit_code: int | str = "timeout" if completed.timed_out else completed.returncode
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        return CommandEvidence(
            name="docker_build",
            command="docker build",
            exit_code=exit_code,
            stdout=str(stdout_path),
            stderr=str(stderr_path),
            phase="setup",
        )

    def _create_container(self, request: TestRunRequest, container_name: str, image: str):
        docker_config = request.config.get("testing", {}).get("docker", {})
        cache_root = Path(str(docker_config.get("cache_root") or "~/.openorchestra/cache/docker")).expanduser()
        if not cache_root.is_absolute():
            cache_root = Path.home() / ".openorchestra" / "cache" / "docker" / cache_root
        cache_root.mkdir(parents=True, exist_ok=True)
        command = [
            self.docker_binary,
            "create",
            "--name",
            container_name,
            "--workdir",
            "/workspace",
            "-v",
            f"{request.repo_dir.resolve()}:/workspace:rw",
            "-v",
            f"{cache_root.resolve()}:/cache:rw",
        ]
        network = self._network_for_phase(request, setup_phase=bool(request.setup_commands))
        if network != "default":
            command.extend(["--network", network])
        if hasattr(os, "getuid") and docker_config.get("userns", "host") == "host":
            command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        command.extend([image, "sleep", "3600"])
        return self.command_runner.run_capture(command, cwd=request.repo_dir, timeout_seconds=request.timeout_seconds)

    def _network_for_phase(self, request: TestRunRequest, *, setup_phase: bool) -> str:
        docker_config = request.config.get("testing", {}).get("docker", {})
        network = str(docker_config.get("network") or "none")
        if network == "install_only":
            return "bridge" if setup_phase else "none"
        if network == "always":
            return "bridge"
        return network

    def _exec_commands(self, request: TestRunRequest, container_name: str, command_texts: tuple[str, ...], *, phase: str) -> list[CommandEvidence]:
        results: list[CommandEvidence] = []
        for index, command in enumerate(command_texts, start=1):
            stdout_path = request.log_dir / f"{phase}_{index}.stdout.log"
            stderr_path = request.log_dir / f"{phase}_{index}.stderr.log"
            completed = self.command_runner.run_capture(
                [self.docker_binary, "exec", container_name, *split_command(command)],
                cwd=request.repo_dir,
                timeout_seconds=request.timeout_seconds,
            )
            exit_code: int | str = "timeout" if completed.timed_out else completed.returncode
            stdout_path.write_text(completed.stdout, encoding="utf-8")
            stderr_path.write_text(completed.stderr, encoding="utf-8")
            results.append(
                CommandEvidence(
                    name=f"{phase}_{index}",
                    command=command,
                    exit_code=exit_code,
                    stdout=str(stdout_path),
                    stderr=str(stderr_path),
                    phase=phase,
                )
            )
            if exit_code != 0:
                break
        return results
