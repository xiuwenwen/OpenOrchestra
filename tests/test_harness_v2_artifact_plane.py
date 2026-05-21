from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult
from harness.agents.runner import AgentPhaseRunner
from harness.artifact_plane import (
    ArtifactDecision,
    ArtifactDecisionStatus,
    ArtifactPlane,
    ArtifactPlaneRepository,
    CanonicalArtifact,
    CanonicalizationChange,
    CanonicalizationReport,
    RawArtifact,
    canonicalization_event,
)
from harness.artifacts.validator import ArtifactValidator
from harness.events import SQLiteEventStore, TraceContext


def test_canonical_artifact_cannot_reuse_raw_artifact_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not reuse raw artifact id"):
        CanonicalArtifact("artifact-1", "artifact-1", "tester_result.json", tmp_path / "canonical.json", "hash")


def test_canonicalization_event_records_audit_changes(tmp_path: Path) -> None:
    raw = RawArtifact("raw-1", "task-1", "environment_contract.json", "planner-1", tmp_path / "raw.json", "raw-hash")
    canonical = CanonicalArtifact("canonical-1", "raw-1", "environment_contract.json", tmp_path / "canonical.json", "canonical-hash")
    change = CanonicalizationChange(
        field_path="dependencies.mode",
        before="repo_setup",
        after="repo_discovery",
        rule_name="contract_mode_alias",
    )
    report = CanonicalizationReport(raw, canonical, changes=(change,))

    event = canonicalization_event(report, trace=TraceContext.start(trace_id="trace-1"))

    assert event.event_type == "ArtifactCanonicalized"
    assert event.task_id == "task-1"
    assert event.payload["canonical_artifact_id"] == "canonical-1"
    assert event.payload["changes"] == [
        {
            "field_path": "dependencies.mode",
            "before": "repo_setup",
            "after": "repo_discovery",
            "rule_name": "contract_mode_alias",
        }
    ]


def test_rejected_artifact_requires_reasons(tmp_path: Path) -> None:
    raw = RawArtifact("raw-2", "task-1", "tester_result.json", "tester-1", tmp_path / "raw.json", "raw-hash")

    with pytest.raises(ValueError, match="rejected reports must include rejection_reasons"):
        CanonicalizationReport(raw, None)

    decision = ArtifactDecision(
        ArtifactDecisionStatus.REJECTED,
        raw_artifact_id="raw-2",
        reasons=("core field contains pending_model_completion",),
    )

    assert decision.status == ArtifactDecisionStatus.REJECTED


def test_artifact_plane_preserves_raw_and_canonicalizes_contract_aliases(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    shutil.copy2(
        Path("tests/fixtures/replay/repo_setup_contract/environment_contract.json"),
        output_dir / "environment_contract.json",
    )
    event_store = SQLiteEventStore(tmp_path / "events.sqlite3")
    plane = ArtifactPlane(
        repository=ArtifactPlaneRepository(tmp_path / "artifact-plane"),
        event_store=event_store,
    )

    result = plane.canonicalize_output_dir(
        task_id="task-artifact-plane",
        producer="planner-1",
        output_dir=output_dir,
        required_outputs=["environment_contract.json"],
        trace=TraceContext.start(trace_id="trace-artifact-plane"),
    )

    assert result.ok
    canonical_artifact = result.reports[0].canonical_artifact
    assert canonical_artifact is not None
    assert result.reports[0].raw_artifact.path.read_text(encoding="utf-8") != canonical_artifact.path.read_text(encoding="utf-8")
    assert {
        (change.field_path, change.before, change.after, change.rule_name)
        for change in result.reports[0].changes
    } == {
        ("setup.mode", "repo_setup", "repo_discovery", "contract_mode_alias"),
        ("dependencies.mode", "install", "repo_discovery", "contract_mode_alias"),
    }
    assert event_store.replay("task-artifact-plane")[0].event_type == "ArtifactCanonicalized"


def test_artifact_plane_rejects_core_pending_template_values_after_marker_cleanup(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    shutil.copy2(
        Path("tests/fixtures/replay/template_residual/tester_result.json"),
        output_dir / "tester_result.json",
    )
    plane = ArtifactPlane(repository=ArtifactPlaneRepository(tmp_path / "artifact-plane"))

    result = plane.canonicalize_output_dir(
        task_id="task-template-residual",
        producer="tester-1",
        output_dir=output_dir,
        required_outputs=["tester_result.json"],
        trace=TraceContext.start(trace_id="trace-template-residual"),
    )

    assert not result.ok
    assert result.reports[0].raw_artifact.path.read_text(encoding="utf-8").count("harness_template_status") == 1
    canonical_payload = json.loads((result.canonical_output_dir / "tester_result.json").read_text(encoding="utf-8"))
    assert "harness_template_status" not in canonical_payload
    assert result.reports[0].rejection_reasons
    assert result.events[0].event_type == "ArtifactRejected"


def test_agent_runner_validates_and_collects_from_artifact_plane_canonical_output(tmp_path: Path) -> None:
    output_dir = tmp_path / "agent-output"
    output_dir.mkdir()
    shutil.copy2(
        Path("tests/fixtures/replay/repo_setup_contract/environment_contract.json"),
        output_dir / "environment_contract.json",
    )
    event_store = SQLiteEventStore(tmp_path / "events.sqlite3")
    artifact_plane = ArtifactPlane(
        repository=ArtifactPlaneRepository(tmp_path / "artifact-plane"),
        event_store=event_store,
    )
    runner = AgentPhaseRunner(
        SimpleNamespace(
            validator=ArtifactValidator(),
            artifact_plane=artifact_plane,
        )
    )
    context = AgentRunContext(
        task_id="task-runner-artifact-plane",
        phase_id="phase-1",
        phase="PLANNING_DRAFT",
        role="planner",
        agent_id="planner-1",
        round_id=0,
        user_prompt="plan",
        role_instruction="plan",
        workspace_dir=tmp_path,
        repo_dir=tmp_path / "repo",
        input_dir=tmp_path / "input",
        output_dir=output_dir,
        log_dir=tmp_path / "logs",
        required_outputs=["environment_contract.json"],
    )

    validation, repaired, _, canonical_output_dir, _ = runner.validate_agent_output(
        AgentRunResult("task-runner-artifact-plane", "phase-1", "planner", "planner-1", "COMPLETED"),
        context,
        "mock",
    )

    assert validation.ok
    assert repaired == ["environment_contract.json"]
    assert canonical_output_dir != output_dir
    assert event_store.replay("task-runner-artifact-plane")[0].event_type == "ArtifactCanonicalized"
