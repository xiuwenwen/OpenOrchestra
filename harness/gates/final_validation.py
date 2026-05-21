from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Callable

from harness.adapters.command_runner import CommandRunner
from harness.artifacts.manager import ArtifactManager
from harness.core.progress import ProgressEvent
from harness.core.taxonomy import FAILURE_TYPES
from harness.runtime.resolver import RuntimeResolver
from harness.runtime.spec import PathMapping, RuntimeSpec
from harness.state.repository import StateRepository
from harness.testing.runners.base import split_command


EXTERNAL_EVALUATOR_RESULT_ARTIFACT = "external_evaluator_result.json"


@dataclass(frozen=True)
class FinalValidationResult:
    status: str
    failure_type: str = "none"
    summary: str = ""
    artifact_path: Path | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def failed(self) -> bool:
        return self.status in {"failed", "blocked"}


class FinalValidationGateService:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        repository: StateRepository,
        artifact_manager: ArtifactManager,
        latest_materialized_repo: Callable[[str], Path | None],
        command_runner: CommandRunner | None = None,
        emit: Callable[[ProgressEvent], None] | None = None,
    ):
        self.config = config
        self.repository = repository
        self.artifact_manager = artifact_manager
        self.latest_materialized_repo = latest_materialized_repo
        self.command_runner = command_runner or CommandRunner()
        self.emit = emit

    def run(self, task_id: str, round_id: int) -> FinalValidationResult:
        final_config = self.config.get("final_validation", {})
        if isinstance(final_config, dict) and final_config.get("enabled") is False:
            return FinalValidationResult(status="skipped", summary="final_validation disabled")

        contract_path = self.latest_validation_contract_path(task_id)
        if contract_path is None:
            return FinalValidationResult(status="skipped", summary="validation_contract.json not found")
        try:
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return self.write_result(
                task_id,
                round_id,
                {
                    "status": "blocked",
                    "failure_type": "contract_bug",
                    "summary": f"validation_contract.json is not valid JSON: {exc}",
                    "validation_contract": str(contract_path),
                    "commands": [],
                },
            )
        if not isinstance(contract, dict):
            return self.write_result(
                task_id,
                round_id,
                {
                    "status": "blocked",
                    "failure_type": "contract_bug",
                    "summary": "validation_contract.json must contain one JSON object",
                    "validation_contract": str(contract_path),
                    "commands": [],
                },
            )

        final_check = contract.get("final_check")
        if not isinstance(final_check, dict):
            return FinalValidationResult(status="skipped", summary="validation_contract.final_check not present")
        final_config_dict = final_config if isinstance(final_config, dict) else {}
        mode = str(final_check.get("mode") or "unknown").strip().lower()
        raw_commands = final_check.get("commands", [])
        commands = [str(command).strip() for command in raw_commands if str(command).strip()] if isinstance(raw_commands, list) else []
        require_executable = bool(final_config_dict.get("require_executable", False))
        if mode in {"none", "unknown", ""} and not commands:
            return FinalValidationResult(status="skipped", summary=f"final_check mode={mode or 'unknown'}")
        if mode == "explicit" and not commands:
            return self.write_result(
                task_id,
                round_id,
                {
                    "status": "blocked",
                    "failure_type": "contract_bug",
                    "summary": "validation_contract.final_check.commands must be non-empty when mode is explicit",
                    "validation_contract": str(contract_path),
                    "commands": [],
                },
            )
        if commands and not bool(final_config_dict.get("allow_contract_commands", False)):
            return self.write_result(
                task_id,
                round_id,
                {
                    "status": "blocked",
                    "failure_type": "contract_bug",
                    "summary": "validation_contract.final_check.commands are not Harness-authorized; set final_validation.allow_contract_commands=true",
                    "authority": str(final_check.get("authority") or ""),
                    "mode": mode,
                    "validation_contract": str(contract_path),
                    "commands": [],
                },
            )
        if not commands:
            if require_executable:
                return self.write_result(
                    task_id,
                    round_id,
                    {
                        "status": "blocked",
                        "failure_type": "contract_bug",
                        "summary": f"final_check mode={mode} has no executable commands",
                        "validation_contract": str(contract_path),
                        "commands": [],
                    },
                )
            return FinalValidationResult(status="skipped", summary=f"final_check mode={mode} has no commands")

        repo_dir = self.latest_materialized_repo(task_id)
        if repo_dir is None:
            return self.write_result(
                task_id,
                round_id,
                {
                    "status": "blocked",
                    "failure_type": "infra",
                    "summary": "No materialized repository exists for final validation",
                    "validation_contract": str(contract_path),
                    "commands": [],
                },
            )

        log_dir = self.artifact_manager.artifact_root / task_id / "context" / "external_evaluator_logs" / f"round_{round_id}"
        log_dir.mkdir(parents=True, exist_ok=True)
        timeout_seconds = self.timeout_seconds(final_config)
        runtime_spec, runtime_repo_dir, runtime_log_dir = self.command_runtime(repo_dir, log_dir)
        command_records: list[dict[str, Any]] = []
        failed_command: dict[str, Any] | None = None
        for index, command in enumerate(commands, start=1):
            expanded = self.expand_command(
                command,
                repo_dir=runtime_repo_dir,
                task_id=task_id,
                round_id=round_id,
                log_dir=runtime_log_dir,
            )
            try:
                argv = split_command(expanded)
                result = self.command_runner.run_capture(
                    argv,
                    cwd=repo_dir,
                    timeout_seconds=timeout_seconds,
                    runtime_spec=runtime_spec,
                )
            except Exception as exc:
                return self.write_result(
                    task_id,
                    round_id,
                    {
                        "status": "blocked",
                        "failure_type": "contract_bug",
                        "summary": f"Final validation command could not be executed: {exc}",
                        "authority": str(final_check.get("authority") or ""),
                        "mode": mode,
                        "validation_contract": str(contract_path),
                        "repository": str(repo_dir),
                        "runtime": self.runtime_payload(runtime_spec, runtime_repo_dir, runtime_log_dir),
                        "commands": [
                            {
                                "command": command,
                                "expanded_command": expanded,
                                "exit_code": None,
                                "error": str(exc),
                            }
                        ],
                    },
                )
            stdout_path = log_dir / f"command_{index}.stdout.log"
            stderr_path = log_dir / f"command_{index}.stderr.log"
            stdout_path.write_text(result.stdout, encoding="utf-8")
            stderr_path.write_text(result.stderr, encoding="utf-8")
            record = {
                "command": command,
                "expanded_command": expanded,
                "exit_code": result.returncode,
                "timed_out": result.timed_out,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }
            record["runtime"] = self.runtime_payload(runtime_spec, runtime_repo_dir, runtime_log_dir)
            command_records.append(record)
            if result.returncode != 0 and failed_command is None:
                failed_command = record
                break

        status = "passed" if failed_command is None else "failed"
        failure_type = "none" if status == "passed" else self.failure_type(final_check, default="source_bug")
        summary = "Final validation passed" if status == "passed" else f"Final validation command failed: {failed_command['expanded_command']}"
        payload: dict[str, Any] = {
            "schema_version": 1,
            "status": status,
            "failure_type": failure_type,
            "summary": summary,
            "authority": str(final_check.get("authority") or ""),
            "mode": mode,
            "validation_contract": str(contract_path),
            "repository": str(repo_dir),
            "runtime": self.runtime_payload(runtime_spec, runtime_repo_dir, runtime_log_dir),
            "commands": command_records,
            "required_result": final_check.get("required_result") if isinstance(final_check.get("required_result"), dict) else {},
        }
        self.apply_json_pass_check(payload, final_check, repo_dir=repo_dir)
        return self.write_result(task_id, round_id, payload)

    def command_runtime(self, repo_dir: Path, log_dir: Path) -> tuple[RuntimeSpec, Path | str, Path | str]:
        spec = RuntimeResolver(self.config).resolve(context="final_validation")
        if not spec.is_docker:
            return spec, repo_dir, log_dir
        runtime_log_dir = "/openorchestra/external_evaluator_logs"
        mounts = (*spec.mounts, PathMapping(log_dir, runtime_log_dir, read_only=False))
        return replace(spec, mounts=mounts), spec.workdir, runtime_log_dir

    def runtime_payload(self, spec: RuntimeSpec, runtime_repo_dir: Path | str, runtime_log_dir: Path | str) -> dict[str, Any]:
        return {
            "mode": spec.mode,
            "image": spec.image,
            "workdir": spec.workdir,
            "network": spec.network,
            "repo_dir": str(runtime_repo_dir),
            "log_dir": str(runtime_log_dir),
        }

    def latest_validation_contract_path(self, task_id: str) -> Path | None:
        for artifact in reversed(self.repository.list_artifacts(task_id, "validation_contract.json")):
            path = Path(artifact["path"])
            if path.exists() and path.is_file():
                return path
        return None

    def timeout_seconds(self, final_config: object) -> int:
        if isinstance(final_config, dict):
            try:
                return max(1, int(final_config.get("timeout_seconds", 1800)))
            except (TypeError, ValueError):
                return 1800
        return 1800

    def failure_type(self, final_check: dict[str, Any], *, default: str) -> str:
        value = str(final_check.get("failure_type") or default).strip().lower()
        return value if value in FAILURE_TYPES else default

    def expand_command(self, command: str, *, repo_dir: Path | str, task_id: str, round_id: int, log_dir: Path | str) -> str:
        replacements = {
            "{repo_dir}": str(repo_dir),
            "{task_id}": task_id,
            "{round_id}": str(round_id),
            "{external_evaluator_log_dir}": str(log_dir),
        }
        expanded = command
        for key, value in replacements.items():
            expanded = expanded.replace(key, value)
        return expanded

    def apply_json_pass_check(self, payload: dict[str, Any], final_check: dict[str, Any], *, repo_dir: Path) -> None:
        result_json_path = str(final_check.get("result_json_path") or "").strip()
        pass_json_path = str(final_check.get("pass_json_path") or "").strip()
        if payload.get("status") != "passed" or not result_json_path or not pass_json_path:
            return
        json_path = Path(result_json_path)
        if not json_path.is_absolute():
            json_path = repo_dir / json_path
        try:
            result_payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            payload.update(
                {
                    "status": "failed",
                    "failure_type": self.failure_type(final_check, default="source_bug"),
                    "summary": f"Final validation result JSON could not be read: {exc}",
                }
            )
            return
        observed = self.get_json_path(result_payload, pass_json_path)
        expected = final_check.get("pass_json_value", True)
        payload["pass_json_observed"] = observed
        payload["pass_json_expected"] = expected
        if observed != expected:
            payload.update(
                {
                    "status": "failed",
                    "failure_type": self.failure_type(final_check, default="source_bug"),
                    "summary": f"Final validation JSON check failed at {pass_json_path}: expected {expected!r}, got {observed!r}",
                }
            )

    def get_json_path(self, payload: Any, path: str) -> Any:
        current = payload
        for part in path.split("."):
            if not part:
                continue
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                current = current[int(part)]
            else:
                return None
        return current

    def write_result(self, task_id: str, round_id: int, payload: dict[str, Any]) -> FinalValidationResult:
        payload.setdefault("schema_version", 1)
        payload.setdefault("commands", [])
        payload.setdefault("failure_type", "none")
        payload.setdefault("summary", "")
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ref = self.artifact_manager.create_text_artifact(
            task_id,
            EXTERNAL_EVALUATOR_RESULT_ARTIFACT,
            content,
            role="orchestrator",
            agent_id="external-evaluator",
        )
        if self.emit is not None:
            self.emit(
                ProgressEvent(
                    "final_validation",
                    task_id=task_id,
                    phase="final_validation",
                    role="orchestrator",
                    agent_id="external-evaluator",
                    round_id=round_id,
                    status=str(payload.get("status") or "failed").upper(),
                    message=str(payload.get("summary") or "final validation completed"),
                    data={
                        "failure_type": str(payload.get("failure_type") or "none"),
                        "artifact": str(ref.path),
                    },
                )
            )
        return FinalValidationResult(
            status=str(payload.get("status") or "failed"),
            failure_type=str(payload.get("failure_type") or "none"),
            summary=str(payload.get("summary") or ""),
            artifact_path=ref.path,
        )
