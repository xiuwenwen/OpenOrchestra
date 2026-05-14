from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CommandEvidence:
    name: str
    command: str
    exit_code: int | str | None
    stdout: str = ""
    stderr: str = ""
    phase: str = "test"
    scope: str = "host"
    host_command: str = ""
    container_command: str = ""
    workdir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "phase": self.phase,
            "scope": self.scope,
            "host_command": self.host_command,
            "container_command": self.container_command,
            "workdir": self.workdir,
        }


@dataclass(frozen=True)
class TestRunEvidence:
    status: str
    runtime: str
    image: str = ""
    project_type: str = "unknown"
    environment_status: str = "skipped"
    build_status: str = "skipped"
    test_status: str = "skipped"
    failure_type: str = "none"
    commands: tuple[CommandEvidence, ...] = ()
    notes: tuple[str, ...] = ()
    cache_key: str | None = None
    cache_hit: bool = False
    cached_from: str = ""
    evidence_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        first_exit_code = self.first_exit_code()
        test_exit_code = self.test_exit_code()
        return {
            "status": self.status,
            "runtime": self.runtime,
            "image": self.image,
            "project_type": self.project_type,
            "environment_status": self.environment_status,
            "build_status": self.build_status,
            "test_status": self.test_status,
            "failure_type": self.failure_type,
            "build_exit_code": first_exit_code,
            "test_exit_code": test_exit_code,
            "cache_key": self.cache_key,
            "cache_hit": self.cache_hit,
            "cached_from": self.cached_from,
            "evidence_path": self.evidence_path,
            "commands": [command.to_dict() for command in self.commands],
            "notes": list(self.notes),
        }

    def first_exit_code(self) -> int | str | None:
        for command in self.commands:
            if command.exit_code is not None:
                return command.exit_code
        return None

    def test_exit_code(self) -> int | str | None:
        test_codes = [command.exit_code for command in self.commands if command.phase == "test" and command.exit_code is not None]
        if not test_codes:
            return self.first_exit_code()
        numeric = [code for code in test_codes if isinstance(code, int)]
        if numeric and all(code == 0 for code in numeric) and len(numeric) == len(test_codes):
            return 0
        return test_codes[0]

    def with_cache(
        self,
        *,
        cache_key: str | None,
        cache_hit: bool = False,
        cached_from: str = "",
        evidence_path: str = "",
    ) -> "TestRunEvidence":
        return TestRunEvidence(
            status=self.status,
            runtime=self.runtime,
            image=self.image,
            project_type=self.project_type,
            environment_status=self.environment_status,
            build_status=self.build_status,
            test_status=self.test_status,
            failure_type=self.failure_type,
            commands=self.commands,
            notes=self.notes,
            cache_key=cache_key,
            cache_hit=cache_hit,
            cached_from=cached_from,
            evidence_path=evidence_path,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TestRunEvidence":
        commands = tuple(
            CommandEvidence(
                name=str(command.get("name") or "command"),
                command=str(command.get("command") or ""),
                exit_code=command.get("exit_code"),
                stdout=str(command.get("stdout") or ""),
                stderr=str(command.get("stderr") or ""),
                phase=str(command.get("phase") or "test"),
                scope=str(command.get("scope") or "host"),
                host_command=str(command.get("host_command") or ""),
                container_command=str(command.get("container_command") or ""),
                workdir=str(command.get("workdir") or ""),
            )
            for command in payload.get("commands", [])
            if isinstance(command, dict)
        )
        return cls(
            status=str(payload.get("status") or "fail"),
            runtime=str(payload.get("runtime") or "native"),
            image=str(payload.get("image") or ""),
            project_type=str(payload.get("project_type") or "unknown"),
            environment_status=str(payload.get("environment_status") or "skipped"),
            build_status=str(payload.get("build_status") or "skipped"),
            test_status=str(payload.get("test_status") or "skipped"),
            failure_type=str(payload.get("failure_type") or "none"),
            commands=commands,
            notes=tuple(str(note) for note in payload.get("notes", []) if str(note).strip()),
            cache_key=payload.get("cache_key"),
            cache_hit=bool(payload.get("cache_hit", False)),
            cached_from=str(payload.get("cached_from") or ""),
            evidence_path=str(payload.get("evidence_path") or ""),
        )


def evidence_from_command_results(
    *,
    runtime: str,
    image: str,
    project_type: str,
    commands: list[CommandEvidence],
    notes: list[str] | None = None,
) -> TestRunEvidence:
    if not commands:
        return TestRunEvidence(
            status="skipped",
            runtime=runtime,
            image=image,
            project_type=project_type,
            environment_status="skipped",
            build_status="skipped",
            test_status="skipped",
            failure_type="none",
            notes=tuple(notes or ()),
        )
    setup_failed = next((command for command in commands if command.phase == "setup" and command.exit_code not in {0, None}), None)
    if setup_failed:
        return TestRunEvidence(
            status="fail",
            runtime=runtime,
            image=image,
            project_type=project_type,
            environment_status="fail",
            build_status="fail",
            test_status="blocked",
            failure_type="env_setup",
            commands=tuple(commands),
            notes=tuple(notes or ()),
        )
    test_commands = [command for command in commands if command.phase == "test"]
    failed_test = next((command for command in test_commands if command.exit_code != 0), None)
    if failed_test and runtime == "docker" and failed_test.exit_code == 126:
        return TestRunEvidence(
            status="fail",
            runtime=runtime,
            image=image,
            project_type=project_type,
            environment_status="fail",
            build_status="pass",
            test_status="blocked",
            failure_type="env_setup",
            commands=tuple(commands),
            notes=tuple(notes or ()),
        )
    return TestRunEvidence(
        status="fail" if failed_test else "pass",
        runtime=runtime,
        image=image,
        project_type=project_type,
        environment_status="pass",
        build_status="pass",
        test_status="fail" if failed_test else "pass",
        failure_type="test" if failed_test else "none",
        commands=tuple(commands),
        notes=tuple(notes or ()),
    )
