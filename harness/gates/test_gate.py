from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from harness.adapters.command_runner import CommandRunner
from harness.artifacts.hashing import sha256_file
from harness.artifacts.manager import ArtifactManager
from harness.core.errors import TaskFailedError
from harness.core.progress import ProgressEvent
from harness.state.repository import StateRepository
from harness.testing.detection import detect_project_profile
from harness.testing.evidence import CommandEvidence, TestRunEvidence
from harness.testing.runners import DockerTestRunner, NativeTestRunner, SweBenchTestRunner, TestRunRequest
from harness.testing.runners.base import split_command


MarkdownFieldReader = Callable[[str, str], str | None]
MaterializedRepoProvider = Callable[[str], Path | None]


class TestGateService:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        repository: StateRepository,
        artifact_manager: ArtifactManager,
        latest_materialized_repo: MaterializedRepoProvider,
        markdown_field: MarkdownFieldReader,
        command_runner: CommandRunner | None = None,
        emit: Callable[[ProgressEvent], None] | None = None,
    ):
        self.config = config
        self.repository = repository
        self.artifact_manager = artifact_manager
        self.latest_materialized_repo = latest_materialized_repo
        self.markdown_field = markdown_field
        self.command_runner = command_runner or CommandRunner()
        self.emit = emit

    def run(self, task_id: str, round_id: int) -> bool:
        return self.run_gate(
            task_id,
            round_id,
            artifact_type="test_gate.md",
            title="Harness Test Gate",
            log_dir_name="test_gate_logs",
            commands=self.harness_test_commands(self.latest_materialized_repo(task_id)),
            require_commands=self.require_harness_test_commands(),
        )

    def run_gate(
        self,
        task_id: str,
        round_id: int,
        *,
        artifact_type: str,
        title: str,
        log_dir_name: str,
        commands: list[str],
        require_commands: bool,
    ) -> bool:
        repo_dir = self.latest_materialized_repo(task_id)
        log_dir = self.artifact_manager.artifact_root / task_id / "context" / log_dir_name / f"round_{round_id}"
        log_dir.mkdir(parents=True, exist_ok=True)
        runtime, runtime_diagnostics = self.resolve_test_runtime_with_diagnostics(task_id, repo_dir)
        self.emit_runtime_selection(task_id, round_id, artifact_type, runtime_diagnostics)
        if repo_dir is None:
            evidence = TestRunEvidence(
                status="fail",
                runtime=runtime,
                environment_status="fail",
                build_status="blocked",
                test_status="blocked",
                failure_type="infra",
                commands=(CommandEvidence(name="repository", command="n/a", exit_code=None, stderr="No materialized repo exists."),),
            )
        elif commands:
            profile = detect_project_profile(repo_dir, self.config)
            setup_commands = self.harness_setup_commands(repo_dir, runtime, profile)
            cache_key = self.test_cache_key(repo_dir, commands, runtime=runtime, image=profile.image, setup_commands=setup_commands)
            cached = self.cached_test_gate_evidence(task_id, cache_key)
            if cached:
                evidence = TestRunEvidence.from_dict(cached).with_cache(
                    cache_key=cache_key,
                    cache_hit=True,
                    cached_from=str(cached.get("artifact_path") or ""),
                )
                report = self.test_gate_report(
                    title,
                    task_id,
                    round_id,
                    repo_dir,
                    evidence.status,
                    [command.to_dict() for command in evidence.commands],
                    evidence=evidence,
                )
                ref = self.artifact_manager.create_text_artifact(
                    task_id,
                    artifact_type,
                    report,
                    role="orchestrator",
                    agent_id=self.agent_id_for_artifact(artifact_type),
                )
                self.emit_gate_event(task_id, round_id, artifact_type, evidence, report_path=str(ref.path))
                return evidence.status == "pass"
            runner = self.runner_for_runtime(runtime)
            evidence = runner.run(
                TestRunRequest(
                    repo_dir=repo_dir,
                    commands=tuple(commands),
                    setup_commands=setup_commands,
                    log_dir=log_dir,
                    timeout_seconds=self.timeout_seconds_for_runtime(runtime),
                    profile=profile,
                    config=self.config,
                    purpose=artifact_type.removesuffix(".md"),
                )
            ).with_cache(cache_key=cache_key)
        elif require_commands:
            evidence = TestRunEvidence(
                status="fail",
                runtime=runtime,
                project_type=detect_project_profile(repo_dir, self.config).project_type,
                environment_status="blocked",
                build_status="blocked",
                test_status="blocked",
                failure_type="infra",
                commands=(CommandEvidence(name="commands", command="n/a", exit_code=None, stderr="No Harness test command configured or detected."),),
            )
        else:
            profile = detect_project_profile(repo_dir, self.config)
            evidence = TestRunEvidence(
                status="skipped",
                runtime=runtime,
                image=profile.image,
                project_type=profile.project_type,
                environment_status="skipped",
                build_status="skipped",
                test_status="skipped",
                failure_type="none",
            )
        evidence_path = log_dir / "evidence.json"
        evidence_path.write_text(json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        evidence = evidence.with_cache(cache_key=evidence.cache_key, cache_hit=evidence.cache_hit, cached_from=evidence.cached_from, evidence_path=str(evidence_path))
        report = self.test_gate_report(
            title,
            task_id,
            round_id,
            repo_dir,
            evidence.status,
            [command.to_dict() for command in evidence.commands],
            evidence=evidence,
        )
        ref = self.artifact_manager.create_text_artifact(
            task_id,
            artifact_type,
            report,
            role="orchestrator",
            agent_id=self.agent_id_for_artifact(artifact_type),
        )
        self.emit_gate_event(task_id, round_id, artifact_type, evidence, report_path=str(ref.path))
        return evidence.status == "pass"

    def harness_test_command_argv(self, command: str) -> list[str]:
        return split_command(command)

    def agent_id_for_artifact(self, artifact_type: str) -> str:
        return "runtime-readiness" if artifact_type == "runtime_readiness.md" else "test-gate"

    def emit_gate_event(self, task_id: str, round_id: int, artifact_type: str, evidence: TestRunEvidence, *, report_path: str) -> None:
        if self.emit is None:
            return
        event_type = "runtime_readiness" if artifact_type == "runtime_readiness.md" else "test_gate"
        self.emit(
            ProgressEvent(
                event_type,
                task_id=task_id,
                phase=artifact_type.removesuffix(".md"),
                role="orchestrator",
                agent_id=self.agent_id_for_artifact(artifact_type),
                round_id=round_id,
                status=evidence.status.upper(),
                message=f"{event_type} {evidence.status}",
                data={
                    "runtime": evidence.runtime,
                    "image": evidence.image,
                    "environment_status": evidence.environment_status,
                    "build_status": evidence.build_status,
                    "test_status": evidence.test_status,
                    "failure_type": evidence.failure_type,
                    "artifact": report_path,
                    "evidence_path": evidence.evidence_path,
                },
            )
        )

    def resolve_test_runtime(self, task_id: str, repo_dir: Path | None) -> str:
        runtime, _diagnostics = self.resolve_test_runtime_with_diagnostics(task_id, repo_dir)
        return runtime

    def resolve_test_runtime_with_diagnostics(self, task_id: str, repo_dir: Path | None) -> tuple[str, dict[str, Any]]:
        testing = self.config.get("testing", {})
        runtime = str(testing.get("runtime") or "auto").strip().lower() if isinstance(testing, dict) else "auto"
        if runtime in {"native", "docker", "swebench"}:
            if runtime == "docker":
                docker = testing.get("docker", {}) if isinstance(testing, dict) else {}
                if isinstance(docker, dict) and docker.get("enabled") is False:
                    return "native", self.runtime_diagnostics(runtime, "native", "docker_disabled")
            return runtime, self.runtime_diagnostics(runtime, runtime, "explicit_runtime")
        if runtime == "auto":
            docker = testing.get("docker", {}) if isinstance(testing, dict) else {}
            if isinstance(docker, dict) and docker.get("enabled") is False:
                return "native", self.runtime_diagnostics(runtime, "native", "docker_disabled")
            docker_ready, docker_data = self.docker_runtime_probe(repo_dir)
            selected = "docker" if docker_ready else "native"
            reason = "docker_available" if docker_ready else str(docker_data.get("reason") or "docker_unavailable")
            diagnostics = self.runtime_diagnostics(runtime, selected, reason)
            diagnostics.update(docker_data)
            return selected, diagnostics
        raise TaskFailedError(f"Invalid testing.runtime: {runtime!r}")

    def docker_runtime_available(self, repo_dir: Path | None) -> bool:
        ready, _diagnostics = self.docker_runtime_probe(repo_dir)
        return ready

    def docker_runtime_probe(self, repo_dir: Path | None) -> tuple[bool, dict[str, Any]]:
        docker_binary = "docker"
        binary_path = shutil.which(docker_binary)
        if binary_path is None:
            return False, {
                "docker_binary": docker_binary,
                "docker_binary_found": False,
                "docker_daemon_ready": False,
                "reason": "docker_binary_missing",
            }
        result = self.command_runner.run_capture(
            [docker_binary, "info", "--format", "{{.ServerVersion}}"],
            cwd=repo_dir or Path.cwd(),
            timeout_seconds=5,
        )
        ready = result.returncode == 0
        return ready, {
            "docker_binary": binary_path,
            "docker_binary_found": True,
            "docker_daemon_ready": ready,
            "docker_probe_exit_code": result.returncode,
            "reason": "docker_available" if ready else "docker_daemon_unavailable",
        }

    def runtime_diagnostics(self, requested: str, selected: str, reason: str) -> dict[str, Any]:
        return {
            "requested_runtime": requested,
            "selected_runtime": selected,
            "runtime_selection_reason": reason,
        }

    def emit_runtime_selection(self, task_id: str, round_id: int, artifact_type: str, diagnostics: dict[str, Any]) -> None:
        if self.emit is None:
            return
        requested = diagnostics.get("requested_runtime", "unknown")
        selected = diagnostics.get("selected_runtime", "unknown")
        reason = diagnostics.get("runtime_selection_reason", "unknown")
        docker_binary = diagnostics.get("docker_binary", "not_checked")
        docker_binary_found = diagnostics.get("docker_binary_found", "not_checked")
        docker_daemon_ready = diagnostics.get("docker_daemon_ready", "not_checked")
        self.emit(
            ProgressEvent(
                "test_runtime_selected",
                task_id=task_id,
                phase=artifact_type.removesuffix(".md"),
                role="orchestrator",
                agent_id=self.agent_id_for_artifact(artifact_type),
                round_id=round_id,
                status=str(selected).upper(),
                message=(
                    "[TEST RUNTIME] "
                    f"requested={requested} selected={selected} "
                    f"docker_binary={docker_binary} docker_binary_found={docker_binary_found} "
                    f"docker_daemon_ready={docker_daemon_ready} reason={reason}"
                ),
                data=diagnostics,
            )
        )

    def runner_for_runtime(self, runtime: str):
        if runtime == "native":
            return NativeTestRunner(self.command_runner)
        if runtime == "docker":
            return DockerTestRunner(self.command_runner)
        if runtime == "swebench":
            return SweBenchTestRunner(DockerTestRunner(self.command_runner))
        raise TaskFailedError(f"Unsupported test runtime: {runtime}")

    def timeout_seconds_for_runtime(self, runtime: str) -> int:
        testing = self.config.get("testing", {})
        if not isinstance(testing, dict):
            return 120
        if runtime == "docker":
            docker = testing.get("docker", {})
            if isinstance(docker, dict) and docker.get("timeout_seconds") is not None:
                return int(docker.get("timeout_seconds") or 120)
        return int(testing.get("timeout_seconds") or 120)

    def require_harness_test_commands(self) -> bool:
        testing = self.config.get("testing", {})
        return bool(testing.get("require_commands", False)) if isinstance(testing, dict) else False

    def harness_test_commands(self, repo_dir: Path | None) -> list[str]:
        testing = self.config.get("testing", {})
        configured = testing.get("commands") if isinstance(testing, dict) else None
        if isinstance(configured, list) and configured:
            return [str(command) for command in configured if str(command).strip()]
        if repo_dir is None:
            return []
        if self.repo_has_pytest_tests(repo_dir):
            return [f"{sys.executable} -m pytest -q"]
        if self.repo_has_python_files(repo_dir):
            return [f"{sys.executable} -m compileall -q ."]
        package_json = repo_dir / "package.json"
        if package_json.exists():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
            scripts = payload.get("scripts") if isinstance(payload, dict) else None
            if isinstance(scripts, dict) and scripts.get("test"):
                return ["npm test"]
            if isinstance(scripts, dict) and scripts.get("build"):
                return ["npm run build"]
        return []

    def harness_setup_commands(self, repo_dir: Path | None, runtime: str, profile) -> tuple[str, ...]:
        testing = self.config.get("testing", {})
        if isinstance(testing, dict):
            configured = testing.get("setup_commands")
            if isinstance(configured, list):
                return tuple(str(command) for command in configured if str(command).strip())
        if runtime == "docker":
            return tuple(profile.setup_commands)
        return ()

    def runtime_readiness_commands(self, repo_dir: Path | None) -> list[str]:
        testing = self.config.get("runtime_readiness", {})
        configured = testing.get("commands") if isinstance(testing, dict) else None
        if isinstance(configured, list) and configured:
            return [str(command) for command in configured if str(command).strip()]
        return self.harness_test_commands(repo_dir)

    def test_cache_key(
        self,
        repo_dir: Path,
        commands: list[str],
        *,
        runtime: str = "native",
        image: str = "",
        setup_commands: tuple[str, ...] = (),
    ) -> str:
        payload = {
            "commands": commands,
            "image": image,
            "repo": self.repo_content_digest(repo_dir),
            "runtime": runtime,
            "setup_commands": list(setup_commands),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def repo_content_digest(self, repo_dir: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(repo_dir.rglob("*")):
            if not path.is_file() or self._ignored_repo_path(path):
                continue
            relative = path.relative_to(repo_dir).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(sha256_file(path).encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    def cached_test_gate_evidence(self, task_id: str, cache_key: str) -> dict[str, Any] | None:
        for artifact in reversed(self.repository.list_artifacts(task_id, "test_gate.md")):
            path = Path(artifact["path"])
            if not path.exists() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            evidence = self.extract_evidence_json(content)
            if evidence.get("cache_key") == cache_key and evidence.get("cache_hit") is not True:
                evidence["artifact_path"] = str(path)
                return evidence
        return None

    def extract_evidence_json(self, content: str) -> dict[str, Any]:
        marker = "## Evidence JSON"
        marker_index = content.find(marker)
        if marker_index < 0:
            return {}
        block = content[marker_index + len(marker) :]
        start = block.find("```json")
        if start < 0:
            return {}
        start = block.find("\n", start)
        end = block.find("```", start)
        if start < 0 or end < 0:
            return {}
        try:
            payload = json.loads(block[start:end].strip())
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def repo_has_pytest_tests(self, repo_dir: Path) -> bool:
        for path in repo_dir.rglob("*.py"):
            if self._ignored_repo_path(path):
                continue
            if path.name.startswith("test_") or path.name.endswith("_test.py") or "tests" in path.parts:
                return True
        return False

    def repo_has_python_files(self, repo_dir: Path) -> bool:
        for path in repo_dir.rglob("*.py"):
            if self._ignored_repo_path(path):
                continue
            return True
        return False

    def _ignored_repo_path(self, path: Path) -> bool:
        ignored_parts = {
            ".git",
            ".openorchestra-cache",
            ".venv",
            ".nox",
            ".tox",
            "__pycache__",
            "deliver",
            "deliveries",
            "env",
            "node_modules",
            "state",
            "venv",
            "workspaces",
        }
        return any(part in ignored_parts for part in path.parts)

    def test_gate_report(
        self,
        title: str,
        task_id: str,
        round_id: int,
        repo_dir: Path | None,
        status: str,
        results: list[dict[str, Any]],
        *,
        evidence: TestRunEvidence | None = None,
    ) -> str:
        evidence_payload = evidence.to_dict() if evidence else self.test_gate_evidence(status, results)
        lines = [
            f"# {title}",
            "",
            f"status: {status}",
            f"task_id: {task_id}",
            f"round_id: {round_id}",
            f"repo_path: {repo_dir or 'none'}",
            f"runtime: {evidence_payload.get('runtime', 'native')}",
            f"image: {evidence_payload.get('image', '') or '-'}",
            f"environment_status: {evidence_payload.get('environment_status', 'skipped')}",
            f"build_status: {evidence_payload.get('build_status', 'skipped')}",
            f"test_status: {evidence_payload.get('test_status', 'skipped')}",
            f"failure_type: {evidence_payload.get('failure_type', 'none')}",
            "",
            "## Evidence JSON",
            "",
            "```json",
            json.dumps(
                evidence_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            "```",
            "",
            "## Commands",
            "",
        ]
        if not results:
            lines.append("- none")
        for result in results:
            lines.extend(
                [
                    f"- command: {result['command']}",
                    f"  exit_code: {result['exit_code'] if result['exit_code'] is not None else 'n/a'}",
                    f"  stdout: {result['stdout'] or '-'}",
                    f"  stderr: {result['stderr'] or '-'}",
                ]
            )
        lines.append("")
        return "\n".join(lines)

    def test_gate_evidence(
        self,
        status: str,
        results: list[dict[str, Any]],
        *,
        cache_key: str | None = None,
        cache_hit: bool = False,
        cached_from: str | None = None,
    ) -> dict[str, Any]:
        exit_codes = [result.get("exit_code") for result in results if result.get("exit_code") is not None]
        numeric_exit_codes = [code for code in exit_codes if isinstance(code, int)]
        first_exit_code = exit_codes[0] if exit_codes else None
        return {
            "status": status,
            "runtime": "native",
            "image": "",
            "project_type": "unknown",
            "environment_status": "pass" if status == "pass" else "fail",
            "build_status": "pass" if status == "pass" else "fail",
            "test_status": "pass" if status == "pass" else "fail",
            "failure_type": "none" if status == "pass" else "test",
            "build_exit_code": first_exit_code,
            "test_exit_code": 0 if numeric_exit_codes and all(code == 0 for code in numeric_exit_codes) else first_exit_code,
            "cache_key": cache_key,
            "cache_hit": cache_hit,
            "cached_from": cached_from or "",
            "evidence_path": "",
            "commands": [
                {
                    "name": result.get("name") or "command",
                    "command": result.get("command"),
                    "exit_code": result.get("exit_code"),
                    "stdout": result.get("stdout"),
                    "stderr": result.get("stderr"),
                    "phase": result.get("phase") or "test",
                }
                for result in results
            ],
            "notes": [],
        }

    def status_for_round(self, task_id: str, round_id: int) -> str | None:
        for artifact in reversed(self.repository.list_artifacts(task_id, "test_gate.md")):
            path = Path(artifact["path"])
            if not path.exists() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if self.markdown_field(content, "round_id") != str(round_id):
                continue
            status = self.markdown_field(content, "status")
            return status.lower() if status else None
        return None
