from __future__ import annotations

from pathlib import Path

from harness.adapters.command_runner import CommandRunner
from harness.testing.evidence import CommandEvidence, TestRunEvidence, evidence_from_command_results
from harness.testing.runners.base import TestRunRequest, split_command


class NativeTestRunner:
    runtime = "native"

    def __init__(self, command_runner: CommandRunner | None = None):
        self.command_runner = command_runner or CommandRunner()

    def run(self, request: TestRunRequest) -> TestRunEvidence:
        if request.repo_dir is None:
            return TestRunEvidence(
                status="fail",
                runtime=self.runtime,
                project_type=request.profile.project_type,
                environment_status="fail",
                build_status="blocked",
                test_status="blocked",
                failure_type="infra",
                commands=(CommandEvidence(name="repository", command="n/a", exit_code=None, stderr="No materialized repo exists."),),
            )
        request.log_dir.mkdir(parents=True, exist_ok=True)
        commands: list[CommandEvidence] = []
        commands.extend(self._run_commands(request, request.setup_commands, phase="setup"))
        if any(command.exit_code != 0 for command in commands):
            return evidence_from_command_results(
                runtime=self.runtime,
                image="",
                project_type=request.profile.project_type,
                commands=commands,
            )
        commands.extend(self._run_commands(request, request.commands, phase="test"))
        return evidence_from_command_results(
            runtime=self.runtime,
            image="",
            project_type=request.profile.project_type,
            commands=commands,
        )

    def _run_commands(self, request: TestRunRequest, command_texts: tuple[str, ...], *, phase: str) -> list[CommandEvidence]:
        results: list[CommandEvidence] = []
        for index, command in enumerate(command_texts, start=1):
            stdout_path = request.log_dir / f"{phase}_{index}.stdout.log"
            stderr_path = request.log_dir / f"{phase}_{index}.stderr.log"
            completed = self.command_runner.run_capture(
                split_command(command),
                cwd=request.repo_dir or Path("."),
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
