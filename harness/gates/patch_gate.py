from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from harness.artifacts.manager import ArtifactManager
from harness.core.progress import ProgressEvent
from harness.core.state_machine import PATCH_MERGE
from harness.patch.gate import (
    PatchGatePolicy,
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
            for phase in self.repository.list_phases(task_id)
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
