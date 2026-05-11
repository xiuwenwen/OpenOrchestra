from __future__ import annotations

import json
from pathlib import Path

from harness.artifacts.manager import ArtifactManager
from harness.artifacts.metadata import ARTIFACT_METADATA_FILENAME, load_artifact_metadata, write_artifact_metadata
from harness.artifacts.validator import ArtifactValidator
from harness.state.db import StateDB
from harness.state.repository import StateRepository


def test_validator_prefers_structured_metadata_over_markdown_fields(tmp_path: Path) -> None:
    (tmp_path / "delivery.md").write_text("not json, but sidecar is authoritative\n", encoding="utf-8")
    (tmp_path / "plan.md").write_text("# Plan without result code\n", encoding="utf-8")
    write_artifact_metadata(
        tmp_path,
        {
            "delivery.md": {"return_code": 0},
            "plan.md": {"artifact_result_code": 0},
        },
    )

    ok, errors = ArtifactValidator().validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok is True
    assert errors == []


def test_validator_rejects_nonzero_structured_metadata_before_markdown_fallback(tmp_path: Path) -> None:
    (tmp_path / "delivery.md").write_text('{"return_code":0}\n', encoding="utf-8")
    (tmp_path / "plan.md").write_text("artifact_result_code: 0\n# Plan\n", encoding="utf-8")
    write_artifact_metadata(tmp_path, {"plan.md": {"artifact_result_code": -1}})

    ok, errors = ArtifactValidator().validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok is False
    assert errors == ["plan.md reports non-zero artifact_result_code: -1"]


def test_artifact_manager_writes_sidecar_metadata_and_does_not_collect_sidecar(tmp_path: Path) -> None:
    repo = StateRepository(StateDB(tmp_path / "state" / "harness.db"))
    task_id = repo.create_task("collect metadata")
    phase_id = repo.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "plan.md").write_text("artifact_result_code: 0\n# Plan\n", encoding="utf-8")
    (output_dir / "delivery.md").write_text('{"return_code":0}\n', encoding="utf-8")
    (output_dir / ARTIFACT_METADATA_FILENAME).write_text(json.dumps({"ignored": True}), encoding="utf-8")

    refs = ArtifactManager(tmp_path / "artifacts", repo).collect_output_dir(
        task_id,
        phase_id,
        "planner",
        "planner-1",
        output_dir,
    )

    assert {ref.artifact_type for ref in refs} == {"delivery.md", "plan.md"}
    plan_ref = next(ref for ref in refs if ref.artifact_type == "plan.md")
    metadata = load_artifact_metadata(plan_ref.path.parent)
    assert metadata["artifacts"]["plan.md"]["artifact_result_code"] == 0
    assert metadata["artifacts"]["plan.md"]["hash"] == plan_ref.hash
