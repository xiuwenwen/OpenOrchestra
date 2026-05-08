from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from harness.artifacts.manager import ArtifactManager
from harness.core.errors import TaskFailedError
from harness.state.repository import StateRepository


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
    ):
        self.config = config
        self.repository = repository
        self.artifact_manager = artifact_manager
        self.latest_materialized_repo = latest_materialized_repo
        self.markdown_field = markdown_field

    def run(self, task_id: str, round_id: int) -> bool:
        repo_dir = self.latest_materialized_repo(task_id)
        commands = self.harness_test_commands(repo_dir)
        log_dir = self.artifact_manager.artifact_root / task_id / "context" / "test_gate_logs" / f"round_{round_id}"
        log_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any]] = []
        status = "skipped"
        if repo_dir is None:
            status = "fail"
            results.append({"command": "n/a", "exit_code": None, "stdout": "", "stderr": "No materialized repo exists."})
        elif commands:
            status = "pass"
            for index, command in enumerate(commands, start=1):
                stdout_path = log_dir / f"command_{index}.stdout.log"
                stderr_path = log_dir / f"command_{index}.stderr.log"
                try:
                    argv = self.harness_test_command_argv(command)
                    completed = subprocess.run(
                        argv,
                        cwd=repo_dir,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=int(self.config.get("testing", {}).get("timeout_seconds", 120)),
                    )
                    exit_code: int | str = completed.returncode
                    stdout = completed.stdout
                    stderr = completed.stderr
                except subprocess.TimeoutExpired as exc:
                    exit_code = "timeout"
                    stdout = self.timeout_output_to_text(exc.stdout)
                    stderr = self.timeout_output_to_text(exc.stderr)
                    stderr = (stderr + "\n" if stderr else "") + f"Command timed out after {exc.timeout}s."
                stdout_path.write_text(stdout, encoding="utf-8")
                stderr_path.write_text(stderr, encoding="utf-8")
                results.append(
                    {
                        "command": command,
                        "exit_code": exit_code,
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                    }
                )
                if exit_code != 0:
                    status = "fail"
        elif self.require_harness_test_commands():
            status = "fail"
            results.append({"command": "n/a", "exit_code": None, "stdout": "", "stderr": "No Harness test command configured or detected."})
        report = self.test_gate_report(task_id, round_id, repo_dir, status, results)
        self.artifact_manager.create_text_artifact(
            task_id,
            "test_gate.md",
            report,
            role="orchestrator",
            agent_id="test-gate",
        )
        return status == "pass"

    def harness_test_command_argv(self, command: str) -> list[str]:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise TaskFailedError(f"Invalid Harness test command: {command!r}: {exc}") from exc
        if not argv:
            raise TaskFailedError("Invalid Harness test command: command is empty")
        return argv

    def timeout_output_to_text(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

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
        if (repo_dir / "tests").exists():
            return [f"{sys.executable} -m pytest -q"]
        if self.repo_has_python_files(repo_dir):
            return [f"{sys.executable} -m compileall -q ."]
        package_json = repo_dir / "package.json"
        if package_json.exists() and shutil.which("npm"):
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

    def repo_has_python_files(self, repo_dir: Path) -> bool:
        for path in repo_dir.rglob("*.py"):
            if any(part in {".venv", "venv", "__pycache__"} for part in path.parts):
                continue
            return True
        return False

    def test_gate_report(
        self,
        task_id: str,
        round_id: int,
        repo_dir: Path | None,
        status: str,
        results: list[dict[str, Any]],
    ) -> str:
        lines = [
            "# Harness Test Gate",
            "",
            f"status: {status}",
            f"task_id: {task_id}",
            f"round_id: {round_id}",
            f"repo_path: {repo_dir or 'none'}",
            "",
            "## Evidence JSON",
            "",
            "```json",
            json.dumps(self.test_gate_evidence(status, results), ensure_ascii=False, indent=2, sort_keys=True),
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

    def test_gate_evidence(self, status: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        exit_codes = [result.get("exit_code") for result in results if result.get("exit_code") is not None]
        numeric_exit_codes = [code for code in exit_codes if isinstance(code, int)]
        first_exit_code = exit_codes[0] if exit_codes else None
        return {
            "status": status,
            "build_exit_code": first_exit_code,
            "test_exit_code": 0 if numeric_exit_codes and all(code == 0 for code in numeric_exit_codes) else first_exit_code,
            "commands": [
                {
                    "command": result.get("command"),
                    "exit_code": result.get("exit_code"),
                    "stdout": result.get("stdout"),
                    "stderr": result.get("stderr"),
                }
                for result in results
            ],
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
