from __future__ import annotations

import os
import shlex
import shutil
import sys
import uuid
from pathlib import Path

from harness.adapters.command_runner import CommandRunner
from harness.runtime.docker import DockerRuntimeExecutor
from harness.runtime.network import docker_network_name, normalize_docker_network
from harness.runtime.spec import RuntimeCommandRequest, RuntimeCommandResult, RuntimeSpec
from harness.testing.evidence import CommandEvidence, TestRunEvidence, evidence_from_command_results
from harness.testing.runners.base import TestCommand, TestRunRequest, normalize_test_command, split_command


class CommandRunnerRuntimeHost:
    def __init__(self, command_runner: CommandRunner):
        self.command_runner = command_runner

    def run_capture(self, request: RuntimeCommandRequest) -> RuntimeCommandResult:
        result = self.command_runner.run_capture(
            list(request.command),
            cwd=request.cwd,
            timeout_seconds=request.timeout_seconds,
            input_text=request.input_text,
            env=dict(request.env or {}) if request.env else None,
        )
        return RuntimeCommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            host_command=request.command,
        )


class DockerTestRunner:
    runtime = "docker"

    def __init__(self, command_runner: CommandRunner | None = None, docker_binary: str = "docker"):
        self.command_runner = command_runner or CommandRunner()
        self.docker_binary = docker_binary
        self.runtime_executor = DockerRuntimeExecutor(
            host_executor=CommandRunnerRuntimeHost(self.command_runner),
            docker_binary=docker_binary,
        )

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
            invalid = self._invalid_container_commands(request)
            if invalid is not None:
                return invalid
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
                            scope="host",
                            host_command=f"docker create {request.profile.image}",
                        ),
                    ),
                )
            created = True
            start_result = self.runtime_executor.start_container(container_name, self._runtime_request(request, image, ()))
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
                            scope="host",
                            host_command=f"docker start {container_name}",
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
            network_switch = self._switch_container_network_for_test(request, container_name)
            if network_switch is not None:
                commands.append(network_switch)
                if network_switch.exit_code != 0:
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
                self.runtime_executor.remove_container(container_name, self._runtime_request(request, image, ()))
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
            scope="host",
            host_command=" ".join(command),
            workdir=str(request.repo_dir),
        )

    def _create_container(self, request: TestRunRequest, container_name: str, image: str):
        runtime_request = self._runtime_request(request, image, ())
        return self.runtime_executor.create_container(runtime_request.spec, request.repo_dir, container_name, runtime_request)

    def _network_for_phase(self, request: TestRunRequest, *, setup_phase: bool) -> str:
        docker_config = request.config.get("testing", {}).get("docker", {})
        field = "testing.docker.setup_network" if setup_phase else "testing.docker.test_network"
        key = "setup_network" if setup_phase else "test_network"
        default = "bridge" if setup_phase else "none"
        return normalize_docker_network(
            docker_config.get(key) if isinstance(docker_config, dict) else None,
            field=field,
            default=default,
        )

    def _switch_container_network_for_test(self, request: TestRunRequest, container_name: str) -> CommandEvidence | None:
        if not request.setup_commands:
            return None
        setup_network = self._network_for_phase(request, setup_phase=True)
        test_network = self._network_for_phase(request, setup_phase=False)
        if docker_network_name(setup_network) == docker_network_name(test_network):
            return None
        stdout_path = request.log_dir / "docker_network_switch.stdout.log"
        stderr_path = request.log_dir / "docker_network_switch.stderr.log"
        command = self._network_switch_command(container_name, setup_network, test_network)
        completed = self.command_runner.run_capture(command, cwd=request.repo_dir, timeout_seconds=30)
        exit_code: int | str = "timeout" if completed.timed_out else completed.returncode
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        return CommandEvidence(
            name="docker_network_switch",
            command=" ".join(command),
            exit_code=exit_code,
            stdout=str(stdout_path),
            stderr=str(stderr_path),
            phase="setup",
            scope="host",
            host_command=" ".join(command),
            workdir=str(request.repo_dir),
        )

    def _network_switch_command(self, container_name: str, setup_network: str, test_network: str) -> list[str]:
        setup_name = docker_network_name(setup_network)
        test_name = docker_network_name(test_network)
        fragments: list[str] = []
        if test_name != "none":
            fragments.append(f"{shlex.quote(self.docker_binary)} network connect {shlex.quote(test_name)} {shlex.quote(container_name)}")
        if setup_name != "none":
            fragments.append(f"{shlex.quote(self.docker_binary)} network disconnect {shlex.quote(setup_name)} {shlex.quote(container_name)}")
        return ["sh", "-lc", " && ".join(fragments) if fragments else "true"]

    def _exec_commands(self, request: TestRunRequest, container_name: str, command_texts: tuple[str | TestCommand, ...], *, phase: str) -> list[CommandEvidence]:
        results: list[CommandEvidence] = []
        for index, raw_command in enumerate(command_texts, start=1):
            test_command = normalize_test_command(raw_command, default_scope="container")
            command = test_command.command
            stdout_path = request.log_dir / f"{phase}_{index}.stdout.log"
            stderr_path = request.log_dir / f"{phase}_{index}.stderr.log"
            host_command = " ".join([self.docker_binary, "exec", container_name, *split_command(command)])
            completed = self.runtime_executor.exec_in_container(
                self._runtime_spec(request, request.profile.image),
                container_name,
                self._runtime_request(request, request.profile.image, tuple(split_command(command))),
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
                    scope="container",
                    host_command=host_command,
                    container_command=command,
                    workdir=self.container_workdir(request),
                )
            )
            if exit_code != 0:
                break
        return results

    def _invalid_container_commands(self, request: TestRunRequest) -> TestRunEvidence | None:
        for phase, command_texts in (("setup", request.setup_commands), ("test", request.commands)):
            for index, raw_command in enumerate(command_texts, start=1):
                command = normalize_test_command(raw_command, default_scope="container")
                reason = self._container_command_violation(request, command)
                if not reason:
                    continue
                return TestRunEvidence(
                    status="fail",
                    runtime=self.runtime,
                    image=request.profile.image,
                    project_type=request.profile.project_type,
                    environment_status="fail",
                    build_status="blocked",
                    test_status="blocked",
                    failure_type="env_setup" if phase == "setup" else "test_command",
                    commands=(
                        CommandEvidence(
                            name=f"{phase}_{index}",
                            command=command.command,
                            exit_code=126,
                            stderr=reason,
                            phase=phase,
                            scope=command.scope,
                            container_command=command.command,
                            workdir=self.container_workdir(request),
                        ),
                    ),
                    notes=(reason,),
                )
        return None

    def _container_command_violation(self, request: TestRunRequest, command: TestCommand) -> str | None:
        if command.scope != "container":
            return f"Docker runtime cannot execute host-scoped command: {command.command}"
        text = command.command
        host_patterns = [
            "/Users/",
            "/private/",
            "/opt/homebrew/",
            ".venv/bin/python",
            sys.executable,
        ]
        if request.repo_dir is not None:
            host_patterns.append(str(request.repo_dir.resolve()))
        home = str(Path.home())
        if home and home != "/":
            host_patterns.append(home)
        for pattern in dict.fromkeys(host_patterns):
            if pattern and pattern in text:
                return f"Docker container command leaks host path {pattern!r}: {text}"
        return None

    def container_workdir(self, request: TestRunRequest) -> str:
        if request.runtime_context is not None:
            return request.runtime_context.container_repo_dir
        return "/workspace"

    def _runtime_request(self, request: TestRunRequest, image: str, command: tuple[str, ...]) -> RuntimeCommandRequest:
        return RuntimeCommandRequest(
            command=command,
            cwd=request.repo_dir,
            timeout_seconds=request.timeout_seconds,
            spec=self._runtime_spec(request, image),
        )

    def _runtime_spec(self, request: TestRunRequest, image: str) -> RuntimeSpec:
        docker_config = request.config.get("testing", {}).get("docker", {})
        cache_root = Path(str(docker_config.get("cache_root") or "~/.openorchestra/cache/docker")).expanduser()
        if not cache_root.is_absolute():
            cache_root = Path.home() / ".openorchestra" / "cache" / "docker" / cache_root
        user = ""
        if hasattr(os, "getuid") and docker_config.get("userns", "host") == "host":
            user = f"{os.getuid()}:{os.getgid()}"
        return RuntimeSpec(
            mode="docker",
            image=image,
            workdir=self.container_workdir(request),
            network=self._network_for_phase(request, setup_phase=bool(request.setup_commands)),
            cache_root=cache_root,
            user=user,
        )
