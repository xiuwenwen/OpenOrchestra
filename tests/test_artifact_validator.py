from __future__ import annotations

from pathlib import Path

from harness.artifacts.validator import ArtifactValidator


def test_artifact_validator_reports_missing_outputs(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("ok", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "risk.md"])

    assert not ok
    assert errors == ["Missing required output: risk.md"]


def test_artifact_validator_accepts_complete_outputs(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("ok", encoding="utf-8")
    (tmp_path / "risk.md").write_text("ok", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("status: success\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "risk.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_rejects_empty_required_output(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "merged_patch.diff").write_text("", encoding="utf-8")
    (tmp_path / "merge_report.md").write_text("ok", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("status: success\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(
        tmp_path, ["merged_patch.diff", "merge_report.md", "delivery.md"]
    )

    assert not ok
    assert errors == ["Required output is empty: merged_patch.diff"]


def test_artifact_validator_accepts_heading_delivery_status(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("ok", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("# Delivery\n\n## Status: success\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_accepts_bold_delivery_status(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("ok", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("**Status**: success\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_rejects_status_with_extra_text(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("ok", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("status: success - complete\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["delivery.md must contain `status: success|failed|partial`"]


def test_artifact_validator_rejects_non_success_delivery_status(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("ok", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("status: partial\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["delivery.md reports non-success status: partial"]


def test_artifact_validator_rejects_delivery_without_status(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("ok", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("# Delivery\nNo status here.\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["delivery.md must contain `status: success|failed|partial`"]


def test_parse_delivery_status_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert ArtifactValidator().parse_delivery_status(tmp_path / "delivery.md") is None
