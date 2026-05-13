from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from harness.artifacts.hashing import sha256_file
from harness.artifacts.manager import ArtifactManager
from harness.core.progress import ProgressEvent
from harness.core.state_machine import EXECUTION, FIXING, PATCH_MERGE, REVIEW_FIXING
from harness.patch.gate import (
    PatchGatePolicy,
    analyze_unified_diff,
    materialized_repo_markdown,
    objective_gate_markdown,
    patch_validation_markdown,
    run_patch_gate,
)
from harness.state.repository import StateRepository


EmitProgress = Callable[[ProgressEvent], None]
PositiveInt = Callable[[Any, int, str], int]
SourceRepoProvider = Callable[[str], Path | None]
MaterializedRepoDirProvider = Callable[[str, int], Path]
CopySource = Callable[[Path, Path], None]
MarkerWriter = Callable[[Path, str, int, Path], None]


class PatchGateService:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        repository: StateRepository,
        artifact_manager: ArtifactManager,
        source_repo_for_task: SourceRepoProvider,
        materialized_repo_dir: MaterializedRepoDirProvider,
        copy_source: CopySource,
        write_success_marker: MarkerWriter,
        emit: EmitProgress,
        positive_int: PositiveInt,
    ):
        self.config = config
        self.repository = repository
        self.artifact_manager = artifact_manager
        self.source_repo_for_task = source_repo_for_task
        self.materialized_repo_dir = materialized_repo_dir
        self.copy_source = copy_source
        self.write_success_marker = write_success_marker
        self.emit = emit
        self.positive_int = positive_int

    def try_deterministic_single_candidate_merge(self, task_id: str, round_id: int) -> bool:
        candidates = self.current_round_candidate_patches(task_id, round_id)
        if len(candidates) != 1:
            return False
        candidate = candidates[0]
        candidate_path = Path(candidate["path"])
        if not candidate_path.exists() or not candidate_path.is_file():
            return False
        phase_id = self.repository.create_phase(task_id, PATCH_MERGE, "executor", round_id)
        run_id = self.repository.create_agent_run(task_id, phase_id, "executor", "deterministic-patch-merge", 0)
        try:
            patch_text = candidate_path.read_text(encoding="utf-8", errors="replace")
            if patch_text and not patch_text.endswith("\n"):
                patch_text += "\n"
            metadata = self.deterministic_merge_metadata(candidate, candidate_path, patch_text, round_id)
            report = self.deterministic_merge_report(candidate, candidate_path, round_id)
            delivery = json.dumps(
                {
                    "return_code": 0,
                    "status": "success",
                    "summary": "Deterministically promoted the single current-round candidate patch.",
                },
                ensure_ascii=False,
                indent=2,
            )
            self.artifact_manager.create_text_artifact(
                task_id,
                "merged_patch.diff",
                patch_text,
                phase_id=phase_id,
                role="executor",
                agent_id="deterministic-patch-merge",
            )
            self.artifact_manager.create_text_artifact(
                task_id,
                "merged_patch_metadata.md",
                metadata,
                phase_id=phase_id,
                role="executor",
                agent_id="deterministic-patch-merge",
            )
            self.artifact_manager.create_text_artifact(
                task_id,
                "merge_report.md",
                report,
                phase_id=phase_id,
                role="executor",
                agent_id="deterministic-patch-merge",
            )
            self.artifact_manager.create_text_artifact(
                task_id,
                "delivery.md",
                delivery + "\n",
                phase_id=phase_id,
                role="executor",
                agent_id="deterministic-patch-merge",
            )
            self.repository.update_agent_run_status(run_id, "COMPLETED")
            self.repository.update_phase_status(phase_id, "COMPLETED")
            self.emit(
                ProgressEvent(
                    "patch_merge_deterministic",
                    task_id=task_id,
                    phase=PATCH_MERGE,
                    role="executor",
                    agent_id="deterministic-patch-merge",
                    round_id=round_id,
                    status="COMPLETED",
                    message="PATCH_MERGE skipped model call because exactly one current-round candidate patch exists",
                    data={"candidate_patch": str(candidate_path), "artifact_type": candidate["artifact_type"]},
                )
            )
        except Exception as exc:
            self.repository.update_agent_run_status(run_id, "FAILED", str(exc))
            self.repository.update_phase_status(phase_id, "FAILED")
            raise
        return True

    def try_skip_noop_candidate_patch(self, task_id: str, round_id: int) -> bool:
        candidates = self.current_round_candidate_patches(task_id, round_id)
        if len(candidates) != 1:
            return False
        candidate = candidates[0]
        candidate_path = Path(candidate["path"])
        if not candidate_path.exists() or not candidate_path.is_file():
            return False
        reason = self.noop_candidate_patch_reason(task_id, round_id, candidate_path)
        if reason is None:
            return False
        phase_id = self.repository.create_phase(task_id, PATCH_MERGE, "orchestrator", round_id, status="FAILED")
        self.artifact_manager.create_text_artifact(
            task_id,
            "patch_validation.md",
            self.noop_patch_validation_report(candidate_path, reason),
            phase_id=phase_id,
            role="orchestrator",
            agent_id="patch-precheck",
        )
        self.artifact_manager.create_text_artifact(
            task_id,
            "materialized_repo.md",
            self.noop_materialized_repo_report(task_id, round_id, candidate_path, reason),
            phase_id=phase_id,
            role="orchestrator",
            agent_id="patch-precheck",
        )
        objective_ref = self.artifact_manager.create_text_artifact(
            task_id,
            "objective_gate.md",
            self.noop_objective_gate_report(task_id, round_id, candidate_path, reason),
            phase_id=phase_id,
            role="orchestrator",
            agent_id="objective-gate",
        )
        self.emit(
            ProgressEvent(
                "patch_noop_skipped",
                task_id=task_id,
                phase=PATCH_MERGE,
                role="orchestrator",
                agent_id="patch-precheck",
                round_id=round_id,
                status="FAILED",
                message=f"PATCH_MERGE skipped testing because candidate patch has no effective change: {reason}",
                data={"candidate_patch": str(candidate_path), "objective_gate": str(objective_ref.path)},
            )
        )
        return True

    def noop_candidate_patch_reason(self, task_id: str, round_id: int, candidate_path: Path) -> str | None:
        patch_text = candidate_path.read_text(encoding="utf-8", errors="replace")
        if not patch_text.strip():
            return "empty_patch"
        candidate_hash = sha256_file(candidate_path)
        previous_hashes = {
            sha256_file(Path(artifact["path"]))
            for artifact in self.repository.list_artifacts(task_id, "merged_patch.diff")
            if Path(artifact["path"]).is_file()
            and artifact.get("phase_id") in self.current_prompt_turn_phase_ids(task_id)
            and self.artifact_round(artifact) is not None
            and int(self.artifact_round(artifact) or -1) < round_id
        }
        if candidate_hash in previous_hashes:
            return "duplicate_previous_merged_patch"
        return None

    def artifact_round(self, artifact: dict[str, Any]) -> int | None:
        phase_id = artifact.get("phase_id")
        if not phase_id:
            return None
        for phase in self.repository.list_phases(artifact["task_id"]):
            if phase["phase_id"] != phase_id or phase.get("round_id") is None:
                continue
            return int(phase["round_id"])
        return None

    def noop_patch_validation_report(self, candidate_path: Path, reason: str) -> str:
        return "\n".join(
            [
                "# Patch Validation",
                "",
                "status: fail",
                f"patch: {candidate_path}",
                f"precheck_status: {reason}",
                "patch_apply_status: skipped",
                "materialize_status: skipped",
                "diff_check_status: skipped",
                "",
                "## stderr",
                "",
                "```text",
                f"Patch precheck failed before merge/materialization: {reason}.",
                "```",
                "",
            ]
        )

    def noop_materialized_repo_report(self, task_id: str, round_id: int, candidate_path: Path, reason: str) -> str:
        return "\n".join(
            [
                "# Materialized Repository",
                "",
                "status: skipped",
                f"task_id: {task_id}",
                f"round_id: {round_id}",
                "repo_path: none",
                f"patch: {candidate_path}",
                "diff_check_status: skipped",
                f"skip_reason: {reason}",
                "",
            ]
        )

    def noop_objective_gate_report(self, task_id: str, round_id: int, candidate_path: Path, reason: str) -> str:
        evidence = {
            "patch_apply_check": False,
            "materialize_status": "skipped",
            "diff_check": False,
            "legal_unified_diff": False,
            "scope_ok": False,
            "size_ok": False,
            "changed_files": [],
            "deleted_files": [],
            "changed_line_count": 0,
            "precheck_errors": [reason],
        }
        return "\n".join(
            [
                "# Objective Gate",
                "",
                "status: fail",
                f"task_id: {task_id}",
                f"round_id: {round_id}",
                f"patch: {candidate_path}",
                f"precheck_status: {reason}",
                "patch_apply_status: skipped",
                "materialize_status: skipped",
                "diff_check_status: skipped",
                "",
                "## Evidence JSON",
                "",
                "```json",
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
                "",
                "## Gate Errors",
                "",
                f"- {reason}",
                "",
            ]
        )

    def current_round_candidate_patches(self, task_id: str, round_id: int) -> list[dict[str, Any]]:
        source_phase_ids = {
            phase["phase_id"]
            for phase in self.current_prompt_turn_phases(task_id)
            if phase["phase_type"] in {EXECUTION, FIXING, REVIEW_FIXING} and phase["round_id"] == round_id
        }
        if not source_phase_ids:
            return []
        candidates: list[dict[str, Any]] = []
        for artifact_type in ("patch.diff", "fix_patch.diff"):
            for artifact in self.repository.list_artifacts(task_id, artifact_type):
                if artifact.get("phase_id") in source_phase_ids and Path(artifact["path"]).is_file():
                    candidates.append(artifact)
        return candidates

    def deterministic_merge_metadata(self, candidate: dict[str, Any], candidate_path: Path, patch_text: str, round_id: int) -> str:
        stats = analyze_unified_diff(patch_text, self.policy())
        changed_files = ", ".join(str(path) for path in stats.changed_files) or "none"
        return "\n".join(
            [
                "artifact_result_code: 0",
                "",
                "# Merged Patch Metadata",
                "",
                "patch_artifact: merged_patch.diff",
                f"selected_candidate_artifacts: {candidate['artifact_type']}",
                f"selected_candidate_path: {candidate_path}",
                f"base_round: {round_id}",
                f"base_task_id: {candidate['task_id']}",
                "base_source_type: current_round_candidate_patch",
                "base_source_path: repository workspace for this PATCH_MERGE round",
                "apply_target: repository_root",
                "patch_scope: merged_authoritative",
                f"changed_files: {changed_files}",
                "expected_apply_command: git apply --whitespace=nowarn merged_patch.diff",
                "compatibility_notes: Single candidate patch promoted without an LLM merge call; objective patch gate remains authoritative.",
                "",
            ]
        )

    def deterministic_merge_report(self, candidate: dict[str, Any], candidate_path: Path, round_id: int) -> str:
        return "\n".join(
            [
                "artifact_result_code: 0",
                "",
                "# Merge Report",
                "",
                "merge_strategy: deterministic_single_candidate",
                f"round_id: {round_id}",
                f"selected_candidate_artifacts: {candidate['artifact_type']}",
                f"selected_candidate_path: {candidate_path}",
                "rejected_candidate_artifacts: none",
                "conflict_handling: not_applicable_single_candidate",
                "ready_for_testing: pending_objective_patch_gate",
                "",
            ]
        )

    def run_validation(self, task_id: str, round_id: int) -> bool:
        latest = self.latest_merged_patch_for_round(task_id, round_id)
        if not latest:
            return False
        patch_path = Path(latest["path"])
        if not patch_path.exists():
            return False
        source_repo = self.source_repo_for_task(task_id)
        gate_result = run_patch_gate(
            patch_path=patch_path,
            source_repo=source_repo,
            materialized_repo_dir=self.materialized_repo_dir(task_id, round_id),
            policy=self.policy(),
            copy_source=self.copy_source,
        )
        if gate_result.materialized_repo:
            self.write_success_marker(gate_result.materialized_repo, task_id, round_id, patch_path)
        report = patch_validation_markdown(gate_result)
        materialize_report = materialized_repo_markdown(gate_result, task_id, round_id)
        objective_report = objective_gate_markdown(gate_result, task_id, round_id)
        ref = self.artifact_manager.create_text_artifact(
            task_id,
            "patch_validation.md",
            report,
            phase_id=latest.get("phase_id"),
            role="orchestrator",
            agent_id="patch-validator",
        )
        materialized_ref = self.artifact_manager.create_text_artifact(
            task_id,
            "materialized_repo.md",
            materialize_report,
            phase_id=latest.get("phase_id"),
            role="orchestrator",
            agent_id="patch-materializer",
        )
        objective_ref = self.artifact_manager.create_text_artifact(
            task_id,
            "objective_gate.md",
            objective_report,
            phase_id=latest.get("phase_id"),
            role="orchestrator",
            agent_id="objective-gate",
        )
        self.emit(
            ProgressEvent(
                "patch_validated",
                task_id=task_id,
                phase=PATCH_MERGE,
                role="orchestrator",
                agent_id="patch-validator",
                round_id=round_id,
                status=gate_result.status.upper(),
                message=f"Objective patch gate {gate_result.status}",
                data={
                    "artifacts": 3,
                    "patch_validation": str(ref.path),
                    "materialized_repo_report": str(materialized_ref.path),
                    "objective_gate": str(objective_ref.path),
                    "materialized_repo": str(gate_result.materialized_repo) if gate_result.materialized_repo else "-",
                },
            )
        )
        return gate_result.status == "pass"

    def policy(self) -> PatchGatePolicy:
        configured = self.config.get("patch_gate", {})
        if not isinstance(configured, dict):
            return PatchGatePolicy()
        return PatchGatePolicy(
            max_changed_lines=self.positive_int(configured.get("max_changed_lines"), 20_000, "patch_gate.max_changed_lines"),
            max_deleted_files=self.positive_int(configured.get("max_deleted_files"), 50, "patch_gate.max_deleted_files"),
        )

    def latest_merged_patch_for_round(self, task_id: str, round_id: int) -> dict[str, Any] | None:
        patch_merge_phase_ids = {
            phase["phase_id"]
            for phase in self.current_prompt_turn_phases(task_id)
            if phase["phase_type"] == PATCH_MERGE and phase["round_id"] == round_id
        }
        if not patch_merge_phase_ids:
            return None
        candidates = [
            artifact
            for artifact in self.repository.list_artifacts(task_id, "merged_patch.diff")
            if artifact.get("phase_id") in patch_merge_phase_ids
        ]
        return candidates[-1] if candidates else None

    def current_prompt_turn_phase_ids(self, task_id: str) -> set[str]:
        return {phase["phase_id"] for phase in self.current_prompt_turn_phases(task_id)}

    def current_prompt_turn_phases(self, task_id: str) -> list[dict[str, Any]]:
        task = self.repository.get_task(task_id)
        prompt_turn_id = int(task["prompt_turn_id"] or 0) if task else 0
        return [
            phase
            for phase in self.repository.list_phases(task_id)
            if int(phase["prompt_turn_id"] or 0) == prompt_turn_id
        ]
