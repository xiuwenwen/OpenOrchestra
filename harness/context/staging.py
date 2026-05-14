from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable

from harness.artifacts.schemas import TEST_REPORT_ARTIFACTS
from harness.artifacts.visibility import ArtifactVisibilityPolicy
from harness.contracts.role_contracts import DEFAULT_ARTIFACT_INPUT_BUDGET, artifact_input_budget_for
from harness.core.state_machine import FAILED, FIXING, REGRESSION_TESTING, REVIEW_FIXING, TEST_JUDGEMENT, TESTING
from harness.judge.judge_runner import MockJudge
from harness.state.repository import StateRepository
from harness.workspace.manager import WorkspaceManager


RepoContextMetadata = Callable[[str, str, str], dict[str, Any]]
PositiveInt = Callable[[Any, int, str], int]


class InputStagingService:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        repository: StateRepository,
        visibility: ArtifactVisibilityPolicy,
        judge: MockJudge,
        repo_context_metadata: RepoContextMetadata,
        positive_int: PositiveInt,
    ):
        self.config = config
        self.repository = repository
        self.visibility = visibility
        self.judge = judge
        self.repo_context_metadata = repo_context_metadata
        self.positive_int = positive_int

    def stage(
        self,
        task_id: str,
        input_dir: Path,
        role: str,
        phase: str,
        exclude_phase_id: str | None = None,
        round_id: int | None = None,
        current_agent_id: str | None = None,
        repo_dir: Path | None = None,
    ) -> list[Path]:
        task = self.repository.get_task(task_id)
        prompt_turn_id = int(task["prompt_turn_id"] or 0) if task else 0
        phases_by_id = {
            phase_row["phase_id"]: phase_row
            for phase_row in self.repository.list_phases(task_id)
            if int(phase_row["prompt_turn_id"] or 0) == prompt_turn_id
        }
        artifacts = [
            artifact
            for artifact in self.repository.list_artifacts(task_id)
            if artifact["phase_id"] is None or artifact["phase_id"] in phases_by_id
        ]
        staged_dir = input_dir / "artifacts"
        staged_dir.mkdir(parents=True, exist_ok=True)
        staged_paths: list[Path] = []
        manifest_lines = ["# Input Artifact Manifest", ""]
        manifest_lines.extend(self.test_target_manifest_lines(task_id, role, phase, round_id, repo_dir))
        limits = self.artifact_input_limits(role, phase)
        staged_file_count = 0
        staged_total_bytes = 0
        visible_artifacts = [
            artifact
            for artifact in artifacts
            if not (exclude_phase_id and artifact["phase_id"] == exclude_phase_id)
        ]
        visible_artifacts = self.visibility.filter_visible_artifacts(
            visible_artifacts,
            phases_by_id,
            role,
            phase,
            round_id,
            current_agent_id=current_agent_id,
        )
        manifest_lines.extend(
            self.testing_failure_context_manifest_lines(
                task_id,
                artifacts,
                phases_by_id,
                role,
                phase,
                round_id,
            )
        )
        for index, artifact in enumerate(reversed(visible_artifacts), start=1):
            source = Path(artifact["path"])
            if not source.exists():
                continue
            source_size = source.stat().st_size
            staging_mode = self.artifact_staging_mode(
                role,
                phase,
                artifact,
                source,
                large_artifact_mode=str(limits["large_artifact_mode"]),
            )
            if staging_mode == "path_only":
                self.append_path_only_artifact_manifest(
                    manifest_lines,
                    index,
                    artifact,
                    source,
                    "large artifact indexed by path only to avoid repeating full content in model context",
                )
                continue
            if staged_file_count >= limits["max_files"]:
                self.append_skipped_artifact_manifest(manifest_lines, index, artifact, source, "max_files exceeded")
                continue
            remaining_total_bytes = limits["max_total_bytes"] - staged_total_bytes
            if remaining_total_bytes <= 0:
                self.append_skipped_artifact_manifest(manifest_lines, index, artifact, source, "max_total_bytes exceeded")
                continue
            safe_type = artifact["artifact_type"].replace("/", "__").replace(" ", "_")
            artifact_role = artifact["role"] or "unknown"
            agent_id = artifact["agent_id"] or "unknown"
            version = artifact["version"]
            destination = staged_dir / f"{index:03d}_{artifact_role}_{agent_id}_{safe_type}_v{version}_{source.name}"
            copied_bytes, truncated = self.copy_artifact_with_budget(
                source,
                destination,
                max_file_bytes=self.artifact_max_file_bytes(limits["max_file_bytes"], staging_mode),
                remaining_total_bytes=remaining_total_bytes,
                artifact=artifact,
            )
            staged_total_bytes += copied_bytes
            staged_file_count += 1
            staged_paths.append(destination)
            manifest_lines.extend(
                [
                    f"## {index}. {artifact['artifact_type']} v{version}",
                    f"- local_path: {destination}",
                    f"- source_path: {source}",
                    f"- role: {artifact_role}",
                    f"- agent_id: {agent_id}",
                    f"- phase_id: {artifact['phase_id']}",
                    f"- source_bytes: {source_size}",
                    f"- staged_bytes: {copied_bytes}",
                    f"- truncated: {str(truncated).lower()}",
                    "",
                ]
            )
        manifest_path = input_dir / "manifest.md"
        manifest_path.write_text("\n".join(manifest_lines), encoding="utf-8")
        return [manifest_path, *staged_paths]

    def test_target_manifest_lines(
        self,
        task_id: str,
        role: str,
        phase: str,
        round_id: int | None,
        repo_dir: Path | None,
    ) -> list[str]:
        if role != "tester" or phase not in {TESTING, REGRESSION_TESTING}:
            return []
        task = self.repository.get_task(task_id) or {}
        repo_metadata = self.repo_context_metadata(task_id, "tester", phase)
        lines = [
            "## Harness Test Target",
            f"- task_id: {task_id}",
            f"- phase: {phase}",
            f"- round_id: {round_id if round_id is not None else 'none'}",
            f"- repository_dir: {repo_dir if repo_dir else 'unavailable'}",
            f"- repository_source_type: {repo_metadata.get('repository_source_type', 'unknown')}",
            f"- repository_source_path: {repo_metadata.get('repository_source_path', 'unavailable')}",
            f"- repository_source_note: {repo_metadata.get('repository_source_note', 'unavailable')}",
            "",
            "## What To Test",
            "",
            "- Treat `repository_dir` as the runnable implementation under test.",
            "- Inspect and run build, unit tests, smoke tests, or static checks directly from `repository_dir` when possible.",
            "- Do not require executor planning notes or patch narrative artifacts to decide the test verdict.",
            "- Compare observable behavior against the original user request below.",
            "",
            "### Original User Request",
            str(task.get("user_prompt") or "unavailable"),
        ]
        if repo_dir and repo_dir.exists():
            lines.extend(["", "### Repository Snapshot"])
            for child in sorted(repo_dir.iterdir(), key=lambda item: item.name)[:30]:
                suffix = "/" if child.is_dir() else ""
                lines.append(f"- {child.name}{suffix}")
        lines.append("")
        return lines

    def test_gate_manifest_lines(self, task_id: str, round_id: int | None) -> list[str]:
        if round_id is None:
            return []
        artifact = self.latest_test_gate_artifact_for_round(task_id, round_id)
        if artifact is None:
            return []
        source = Path(artifact["path"])
        if not source.exists():
            return []
        content = source.read_text(encoding="utf-8", errors="replace")
        status = self.markdown_field(content, "status") or "unknown"
        evidence = self.extract_evidence_json(content)
        lines = [
            "",
            "## Harness Test Gate Evidence",
            f"- test_gate_status: {status}",
            f"- test_gate_artifact_path: {source}",
            f"- test_gate_runtime: {evidence.get('runtime', 'unknown')}",
            f"- test_gate_image: {evidence.get('image') or '-'}",
            f"- test_gate_evidence_path: {evidence.get('evidence_path') or '-'}",
            f"- test_gate_failure_type: {evidence.get('failure_type', 'unknown')}",
            "- note: This is prior Harness gate evidence for this round, usually from an earlier tester environment repair attempt.",
            "- runtime_blocker_rule: If failure_type is infra, env_setup, or test_command, treat the evidence as a runtime/test-command blocker rather than proof that the source patch is wrong unless repository evidence proves otherwise.",
            "- tester_instruction: Use this legacy evidence only as context; tester_result.json remains the current decision contract.",
        ]
        commands = self.test_gate_commands(content)
        if commands:
            lines.append("- commands_already_run:")
            lines.extend(f"  - {command}" for command in commands)
        return lines

    def latest_test_gate_artifact_for_round(self, task_id: str, round_id: int) -> dict[str, Any] | None:
        matching: list[dict[str, Any]] = []
        for artifact in self.repository.list_artifacts(task_id, "test_gate.md"):
            source = Path(artifact["path"])
            if not source.exists():
                continue
            content = source.read_text(encoding="utf-8", errors="replace")
            if self.markdown_field(content, "round_id") == str(round_id):
                matching.append(artifact)
        if not matching:
            return None
        return max(matching, key=lambda item: int(item.get("version") or 0))

    def markdown_field(self, content: str, field_name: str) -> str | None:
        prefix = f"{field_name}:"
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith(prefix.lower()):
                return stripped.split(":", 1)[1].strip()
        return None

    def test_gate_commands(self, content: str) -> list[str]:
        commands: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("- command:"):
                command = stripped.split(":", 1)[1].strip()
                if command and command != "n/a":
                    commands.append(command)
        return commands

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

    def testing_failure_context_manifest_lines(
        self,
        task_id: str,
        artifacts: list[dict[str, Any]],
        phases_by_id: dict[str, dict[str, Any]],
        role: str,
        phase: str,
        round_id: int | None,
    ) -> list[str]:
        if role != "executor" or phase not in {FIXING, REVIEW_FIXING} or round_id is None:
            return []
        failed_rounds = self.failed_test_rounds_before(task_id, phases_by_id, round_id)
        if not failed_rounds:
            return []
        tester_artifacts_by_round: dict[int, set[str]] = {}
        for artifact in artifacts:
            if (artifact.get("role") or "") != "tester" or artifact.get("artifact_type") not in TEST_REPORT_ARTIFACTS:
                continue
            phase_row = phases_by_id.get(artifact.get("phase_id") or "")
            if not phase_row or phase_row.get("round_id") is None:
                continue
            artifact_round = int(phase_row["round_id"])
            if artifact_round >= round_id:
                continue
            tester_artifacts_by_round.setdefault(artifact_round, set()).add(str(artifact["artifact_type"]))
        complete_visible_rounds = [
            test_round
            for test_round, artifact_types in tester_artifacts_by_round.items()
            if TEST_REPORT_ARTIFACTS <= artifact_types
        ]
        latest_visible_round = max(complete_visible_rounds, default=None)
        unavailable_failed_rounds = [
            test_round
            for test_round in failed_rounds
            if not TEST_REPORT_ARTIFACTS <= tester_artifacts_by_round.get(test_round, set())
        ]
        lines = [
            "## Harness Test Failure Context",
            f"- failed_test_round_count_before_current: {len(failed_rounds)}",
            f"- failed_test_round_ids_before_current: {', '.join(str(value) for value in failed_rounds)}",
        ]
        if latest_visible_round is not None:
            lines.append(f"- latest_visible_complete_test_evidence_round: {latest_visible_round}")
        else:
            lines.append("- latest_visible_complete_test_evidence_round: none")
        if unavailable_failed_rounds:
            lines.append(
                "- failed_test_rounds_without_complete_visible_reports: "
                + ", ".join(str(value) for value in unavailable_failed_rounds)
            )
            lines.append(
                "- evidence_note: Some failed test rounds did not publish complete tester report artifacts; "
                "use the latest visible test reports together with the current repo state."
            )
        lines.append("")
        return lines

    def failed_test_rounds_before(
        self,
        task_id: str,
        phases_by_id: dict[str, dict[str, Any]],
        round_id: int,
    ) -> list[int]:
        failed_rounds: set[int] = set()
        for phase_row in phases_by_id.values():
            if phase_row.get("task_id") != task_id:
                continue
            if phase_row.get("phase_type") not in {TESTING, REGRESSION_TESTING}:
                continue
            if phase_row.get("round_id") is None or int(phase_row["round_id"]) >= round_id:
                continue
            if phase_row.get("status") == FAILED:
                failed_rounds.add(int(phase_row["round_id"]))
        for decision in self.repository.list_judge_decisions(task_id):
            if decision.get("decision_type") != TEST_JUDGEMENT:
                continue
            phase_row = phases_by_id.get(decision.get("phase_id") or "")
            if not phase_row or phase_row.get("round_id") is None:
                continue
            decision_round = int(phase_row["round_id"])
            if decision_round >= round_id:
                continue
            try:
                payload = json.loads(decision["decision_payload"])
            except Exception:
                failed_rounds.add(decision_round)
                continue
            if not self.judge.is_test_pass(payload):
                failed_rounds.add(decision_round)
        return sorted(failed_rounds)

    def artifact_input_limits(self, role: str | None = None, phase: str | None = None) -> dict[str, Any]:
        configured = self.config.get("artifact_input", {})
        if not isinstance(configured, dict):
            configured = {}
        contract_budget = artifact_input_budget_for(role, phase) if role and phase else DEFAULT_ARTIFACT_INPUT_BUDGET
        role_phase_config = self.role_phase_artifact_input_config(configured, role, phase)
        max_files = self.positive_int(
            role_phase_config.get("max_files"),
            contract_budget.max_files,
            self.artifact_input_budget_config_name(role, phase, "max_files"),
        )
        max_file_bytes = self.positive_int(
            role_phase_config.get("max_file_bytes"),
            contract_budget.max_file_bytes,
            self.artifact_input_budget_config_name(role, phase, "max_file_bytes"),
        )
        max_total_bytes = self.positive_int(
            role_phase_config.get("max_total_bytes"),
            contract_budget.max_total_bytes,
            self.artifact_input_budget_config_name(role, phase, "max_total_bytes"),
        )
        if "max_files" in configured:
            max_files = min(
                max_files,
                self.positive_int(configured.get("max_files"), 50, "artifact_input.max_files"),
            )
        if "max_file_bytes" in configured:
            max_file_bytes = min(
                max_file_bytes,
                self.positive_int(configured.get("max_file_bytes"), 262_144, "artifact_input.max_file_bytes"),
            )
        if "max_total_bytes" in configured:
            max_total_bytes = min(
                max_total_bytes,
                self.positive_int(configured.get("max_total_bytes"), 1_048_576, "artifact_input.max_total_bytes"),
            )
        large_artifact_mode = str(role_phase_config.get("large_artifact_mode") or contract_budget.large_artifact_mode)
        if large_artifact_mode not in {"auto", "copy", "path_only", "truncated"}:
            large_artifact_mode = contract_budget.large_artifact_mode
        return {
            "max_files": max_files,
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
            "large_artifact_mode": large_artifact_mode,
        }

    def role_phase_artifact_input_config(
        self,
        configured: dict[str, Any],
        role: str | None,
        phase: str | None,
    ) -> dict[str, Any]:
        if not role or not phase:
            return {}
        role_phase = configured.get("role_phase")
        if not isinstance(role_phase, dict):
            return {}
        direct = role_phase.get(f"{role}:{phase}")
        if isinstance(direct, dict):
            return direct
        role_entry = role_phase.get(role)
        if not isinstance(role_entry, dict):
            return {}
        phase_entry = role_entry.get(phase) or role_entry.get(phase.lower()) or role_entry.get("*")
        return phase_entry if isinstance(phase_entry, dict) else {}

    def artifact_input_budget_config_name(self, role: str | None, phase: str | None, key: str) -> str:
        if role and phase:
            return f"artifact_input.role_phase.{role}.{phase}.{key}"
        return f"artifact_input.{key}"

    def copy_artifact_with_budget(
        self,
        source: Path,
        destination: Path,
        *,
        max_file_bytes: int,
        remaining_total_bytes: int,
        artifact: dict[str, Any] | None = None,
    ) -> tuple[int, bool]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_size = source.stat().st_size
        allowed_bytes = min(max_file_bytes, remaining_total_bytes)
        if source_size <= allowed_bytes:
            WorkspaceManager.copy_file_fast(source, destination)
            return source_size, False
        cached = self.cached_truncated_artifact(source, allowed_bytes, artifact)
        if cached:
            WorkspaceManager.copy_file_fast(cached, destination)
            return destination.stat().st_size, True
        raw = source.read_bytes()
        if allowed_bytes <= 128:
            selected = raw[:allowed_bytes]
        else:
            marker = b"\n\n...[artifact truncated by Harness input budget]...\n\n"
            head_size = max(1, (allowed_bytes - len(marker)) // 2)
            tail_size = max(1, allowed_bytes - len(marker) - head_size)
            selected = raw[:head_size] + marker + raw[-tail_size:]
        destination.write_text(selected.decode("utf-8", errors="replace"), encoding="utf-8")
        self.store_truncated_artifact_cache(source, allowed_bytes, artifact, destination)
        return destination.stat().st_size, True

    def cached_truncated_artifact(self, source: Path, allowed_bytes: int, artifact: dict[str, Any] | None) -> Path | None:
        cache_path = self.truncated_artifact_cache_path(source, allowed_bytes, artifact)
        if not cache_path.exists() or not cache_path.is_file():
            return None
        return cache_path

    def store_truncated_artifact_cache(
        self,
        source: Path,
        allowed_bytes: int,
        artifact: dict[str, Any] | None,
        staged_source: Path,
    ) -> None:
        cache_path = self.truncated_artifact_cache_path(source, allowed_bytes, artifact)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_path.exists():
            return
        temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
        shutil.copy2(staged_source, temporary)
        temporary.replace(cache_path)

    def truncated_artifact_cache_path(self, source: Path, allowed_bytes: int, artifact: dict[str, Any] | None) -> Path:
        cache_root = self.input_staging_cache_root()
        digest = hashlib.sha256()
        digest.update(str(source.resolve()).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(source.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(source.stat().st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(allowed_bytes).encode("ascii"))
        digest.update(b"\0")
        if artifact:
            digest.update(str(artifact.get("artifact_id") or "").encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(artifact.get("version") or "").encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(artifact.get("hash") or "").encode("utf-8"))
        return cache_root / f"{digest.hexdigest()}.artifact"

    def input_staging_cache_root(self) -> Path:
        system = self.config.get("system", {})
        artifact_root = system.get("artifact_root") if isinstance(system, dict) else None
        root = Path(str(artifact_root or ".artifacts")).expanduser()
        return root.resolve() / "_input_staging_cache"

    def append_skipped_artifact_manifest(
        self,
        manifest_lines: list[str],
        index: int,
        artifact: dict[str, Any],
        source: Path,
        reason: str,
    ) -> None:
        manifest_lines.extend(
            [
                f"## {index}. {artifact['artifact_type']} v{artifact['version']}",
                f"- skipped: true",
                f"- reason: {reason}",
                f"- source_path: {source}",
                f"- role: {artifact['role'] or 'unknown'}",
                f"- agent_id: {artifact['agent_id'] or 'unknown'}",
                f"- phase_id: {artifact['phase_id']}",
                f"- source_bytes: {source.stat().st_size}",
                "",
            ]
        )

    def append_path_only_artifact_manifest(
        self,
        manifest_lines: list[str],
        index: int,
        artifact: dict[str, Any],
        source: Path,
        reason: str,
    ) -> None:
        manifest_lines.extend(
            [
                f"## {index}. {artifact['artifact_type']} v{artifact['version']}",
                f"- local_path: path_only",
                f"- full_content_staged: false",
                f"- reason: {reason}",
                f"- source_path: {source}",
                f"- role: {artifact['role'] or 'unknown'}",
                f"- agent_id: {artifact['agent_id'] or 'unknown'}",
                f"- phase_id: {artifact['phase_id']}",
                f"- source_bytes: {source.stat().st_size}",
                "",
            ]
        )

    def artifact_staging_mode(
        self,
        role: str,
        phase: str,
        artifact: dict[str, Any],
        source: Path,
        large_artifact_mode: str = "auto",
    ) -> str:
        if artifact["artifact_type"] != "merged_patch.diff":
            return "copy"
        if source.stat().st_size < 64_000:
            return "copy"
        if large_artifact_mode != "auto":
            return large_artifact_mode
        if role in {"tester", "judge", "communicator"}:
            return "path_only"
        if role == "reviewer":
            return "truncated"
        if role == "executor" and phase in {FIXING, REVIEW_FIXING}:
            return "truncated"
        return "copy"

    def artifact_max_file_bytes(self, configured_max_file_bytes: int, staging_mode: str) -> int:
        if staging_mode == "truncated":
            return min(configured_max_file_bytes, 16_384)
        return configured_max_file_bytes
