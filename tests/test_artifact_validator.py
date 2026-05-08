from __future__ import annotations

from pathlib import Path

from harness.artifacts.delivery_codes import DELIVERY_RETURN_CODE_BY_CODE, DELIVERY_RETURN_CODES
from harness.artifacts.validator import ArtifactValidator


def md_ok(text: str = "ok") -> str:
    return f"artifact_result_code: 0\n\n{text}"


def test_artifact_validator_reports_missing_outputs(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "risk.md"])
    result = validator.validate_required_outputs_result(tmp_path, ["plan.md", "risk.md"])

    assert not ok
    assert errors == ["Missing required output: risk.md"]
    assert result.ok is False
    assert result.issues[0].artifact == "risk.md"
    assert result.issues[0].code == "missing_required_output"


def test_artifact_validator_accepts_complete_outputs(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "risk.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "risk.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_accepts_blank_lines_before_raw_delivery_return_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("\n\nreturn_code: 0\n# Delivery\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_rejects_empty_required_output(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "merged_patch.diff").write_text("", encoding="utf-8")
    (tmp_path / "merge_report.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(
        tmp_path, ["merged_patch.diff", "merge_report.md", "delivery.md"]
    )

    assert not ok
    assert errors == ["Required output is empty: merged_patch.diff"]


def test_artifact_validator_accepts_delivery_return_code_anywhere(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("# Delivery\n\nreturn_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_accepts_bold_delivery_return_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("**return_code**: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_rejects_return_code_with_extra_text(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0 - complete\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["delivery.md must contain `return_code: <int>`"]


def test_artifact_validator_rejects_non_zero_delivery_return_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 1\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["delivery.md reports non-zero return_code: 1"]


def test_artifact_validator_rejects_legacy_delivery_status(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("status: success\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["delivery.md must contain `return_code: <int>`"]


def test_artifact_validator_rejects_delivery_without_return_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text(md_ok(), encoding="utf-8")
    (tmp_path / "delivery.md").write_text("# Delivery\nNo status here.\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["delivery.md must contain `return_code: <int>`"]


def test_artifact_validator_rejects_markdown_without_artifact_result_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["plan.md must contain `artifact_result_code: <int>`"]


def test_artifact_validator_accepts_markdown_artifact_result_code_anywhere(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("# Plan\n\nartifact_result_code: 0\n\nBody\n", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_accepts_bold_markdown_artifact_result_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("# Plan\n\n- **artifact_result_code**: **0**\n\nBody\n", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert ok
    assert errors == []


def test_artifact_validator_rejects_non_zero_markdown_artifact_result_code(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    (tmp_path / "plan.md").write_text("artifact_result_code: 1\n\n# Plan\n", encoding="utf-8")
    (tmp_path / "delivery.md").write_text("return_code: 0\n", encoding="utf-8")

    ok, errors = validator.validate_required_outputs(tmp_path, ["plan.md", "delivery.md"])

    assert not ok
    assert errors == ["plan.md reports non-zero artifact_result_code: 1"]


def test_parse_delivery_status_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert ArtifactValidator().parse_delivery_status(tmp_path / "delivery.md") is None


def test_parse_delivery_status_maps_return_codes(tmp_path: Path) -> None:
    validator = ArtifactValidator()
    delivery = tmp_path / "delivery.md"

    delivery.write_text("return_code: 0\n", encoding="utf-8")
    assert validator.parse_delivery_status(delivery) == "success"

    delivery.write_text("return_code: 2\n", encoding="utf-8")
    assert validator.parse_delivery_status(delivery) == "partial"

    delivery.write_text("return_code: -3\n", encoding="utf-8")
    assert validator.parse_delivery_status(delivery) == "failed"


def test_delivery_return_codes_have_canonical_meanings() -> None:
    assert {entry.code for entry in DELIVERY_RETURN_CODES} == {0, 1, 2, 3, -1, -2, -3}
    assert DELIVERY_RETURN_CODE_BY_CODE[0].status == "success"
    assert DELIVERY_RETURN_CODE_BY_CODE[1].label == "partial"
    assert DELIVERY_RETURN_CODE_BY_CODE[2].label == "blocked"
    assert DELIVERY_RETURN_CODE_BY_CODE[3].label == "degraded"
    assert DELIVERY_RETURN_CODE_BY_CODE[-1].label == "unusable"
    assert DELIVERY_RETURN_CODE_BY_CODE[-2].label == "invalid_outputs"
    assert DELIVERY_RETURN_CODE_BY_CODE[-3].label == "runtime_error"
