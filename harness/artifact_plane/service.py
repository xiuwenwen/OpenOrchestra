from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import uuid

from harness.artifact_plane.model import (
    CanonicalArtifact,
    CanonicalizationChange,
    CanonicalizationReport,
    RawArtifact,
    canonicalization_event,
)
from harness.artifacts.hashing import sha256_file
from harness.artifacts.validator import ArtifactValidator, ValidationRepair, ValidationResult
from harness.events import EventEnvelope, EventStore, TraceContext


@dataclass(frozen=True)
class ArtifactPlaneResult:
    task_id: str
    canonical_output_dir: Path
    validation_result: ValidationResult
    reports: tuple[CanonicalizationReport, ...]
    events: tuple[EventEnvelope, ...]

    @property
    def ok(self) -> bool:
        return self.validation_result.ok


class ArtifactPlaneRepository:
    """Stores raw evidence separately from repaired canonical evidence."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def new_canonical_output_dir(self, task_id: str) -> Path:
        output_dir = self.root / task_id / "canonical_runs" / str(uuid.uuid4()) / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def store_raw(self, *, task_id: str, producer: str, artifact_type: str, source_path: Path) -> RawArtifact:
        artifact_id = f"raw-{uuid.uuid4()}"
        destination = self.root / task_id / "raw" / f"{artifact_id}-{self._safe_name(artifact_type)}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        return RawArtifact(
            artifact_id=artifact_id,
            task_id=task_id,
            artifact_type=artifact_type,
            producer=producer,
            path=destination,
            content_hash=sha256_file(destination),
        )

    def canonical_artifact(
        self,
        *,
        raw: RawArtifact,
        artifact_type: str,
        canonical_path: Path,
    ) -> CanonicalArtifact:
        return CanonicalArtifact(
            artifact_id=f"canonical-{uuid.uuid4()}",
            raw_artifact_id=raw.artifact_id,
            artifact_type=artifact_type,
            path=canonical_path,
            content_hash=sha256_file(canonical_path),
        )

    def _safe_name(self, artifact_type: str) -> str:
        return artifact_type.replace("/", "_").replace("\\", "_")


class ArtifactPlane:
    def __init__(
        self,
        *,
        repository: ArtifactPlaneRepository,
        validator: ArtifactValidator | None = None,
        event_store: EventStore | None = None,
        max_repair_passes: int = 3,
    ) -> None:
        self.repository = repository
        self.validator = validator or ArtifactValidator()
        self.event_store = event_store
        self.max_repair_passes = max_repair_passes

    def canonicalize_output_dir(
        self,
        *,
        task_id: str,
        producer: str,
        output_dir: Path,
        required_outputs: list[str],
        trace: TraceContext,
    ) -> ArtifactPlaneResult:
        canonical_dir = self.repository.new_canonical_output_dir(task_id)
        self._copy_output_tree(output_dir, canonical_dir)

        raw_by_artifact: dict[str, RawArtifact] = {}
        for artifact_type in required_outputs:
            source_path = output_dir / artifact_type
            if source_path.is_file():
                raw_by_artifact[artifact_type] = self.repository.store_raw(
                    task_id=task_id,
                    producer=producer,
                    artifact_type=artifact_type,
                    source_path=source_path,
                )

        validation = self.validator.validate_required_outputs_result(canonical_dir, required_outputs)
        repairs: list[ValidationRepair] = []
        for _ in range(self.max_repair_passes):
            current_repairs = self.validator.repair_trivial_contract_issues_detailed(canonical_dir, validation)
            if not current_repairs:
                break
            repairs.extend(current_repairs)
            validation = self.validator.validate_required_outputs_result(canonical_dir, required_outputs)
            if validation.ok:
                break

        reports = self._build_reports(
            task_id=task_id,
            canonical_dir=canonical_dir,
            required_outputs=required_outputs,
            raw_by_artifact=raw_by_artifact,
            repairs=repairs,
            validation=validation,
        )
        events = tuple(canonicalization_event(report, trace=trace.child()) for report in reports)
        if self.event_store is not None:
            self.event_store.append_many(events)
        return ArtifactPlaneResult(
            task_id=task_id,
            canonical_output_dir=canonical_dir,
            validation_result=validation,
            reports=reports,
            events=events,
        )

    def _build_reports(
        self,
        *,
        task_id: str,
        canonical_dir: Path,
        required_outputs: list[str],
        raw_by_artifact: dict[str, RawArtifact],
        repairs: list[ValidationRepair],
        validation: ValidationResult,
    ) -> tuple[CanonicalizationReport, ...]:
        reports: list[CanonicalizationReport] = []
        issues_by_artifact: dict[str, list[str]] = {}
        for issue in validation.issues:
            if issue.severity == "error":
                issues_by_artifact.setdefault(issue.artifact, []).append(issue.message)
        for artifact_type in required_outputs:
            raw = raw_by_artifact.get(artifact_type)
            if raw is None:
                raw = RawArtifact(
                    artifact_id=f"raw-missing-{uuid.uuid4()}",
                    task_id=task_id,
                    artifact_type=artifact_type,
                    producer="artifact-plane",
                    path=canonical_dir / artifact_type,
                    content_hash="missing",
                )
            changes = tuple(self._repair_to_change(repair) for repair in repairs if repair.artifact == artifact_type)
            canonical_path = canonical_dir / artifact_type
            reasons = tuple(issues_by_artifact.get(artifact_type, ()))
            if canonical_path.is_file() and not reasons:
                canonical = self.repository.canonical_artifact(
                    raw=raw,
                    artifact_type=artifact_type,
                    canonical_path=canonical_path,
                )
                reports.append(CanonicalizationReport(raw, canonical, changes=changes))
            else:
                reports.append(
                    CanonicalizationReport(
                        raw,
                        None,
                        changes=changes,
                        rejection_reasons=reasons or ("required artifact was not produced",),
                    )
                )
        return tuple(reports)

    def _repair_to_change(self, repair: ValidationRepair) -> CanonicalizationChange:
        return CanonicalizationChange(
            field_path=repair.field_path,
            before=repair.before,
            after=repair.after,
            rule_name=repair.rule_name,
        )

    def _copy_output_tree(self, source: Path, destination: Path) -> None:
        for child in source.iterdir():
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            elif child.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)
