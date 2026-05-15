from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from harness.artifacts.delivery_codes import DELIVERY_SUCCESS_RETURN_CODE, delivery_status_for_return_code
from harness.artifacts.metadata import load_artifact_metadata, metadata_int_field
from harness.artifacts.output_templates import output_has_pending_template_marker
from harness.artifacts.acceptance import validate_acceptance_oracles, validate_tester_oracle_results
from harness.artifacts.peer_review import (
    PEER_REVIEW_RESULT_ARTIFACT,
    parse_peer_review_result_content,
    validate_peer_review_result_payload,
)
from harness.artifacts.review_decision import REVIEW_RESULT_ARTIFACT, parse_review_result_content, validate_review_result_payload

RETURN_CODE_FIELD_PATTERN = re.compile(r"^\s*[\-\*\s]*\**return_code\**\s*:\s*\**(-?\d+)\**\s*$")
ARTIFACT_RESULT_CODE_FIELD_PATTERN = re.compile(r"^\s*[\-\*\s]*\**artifact_result_code\**\s*:\s*\**(-?\d+)\**\s*$")


@dataclass(frozen=True)
class ValidationIssue:
    artifact: str
    code: str
    message: str
    severity: str = "error"


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> list[str]:
        return [issue.message for issue in self.issues if issue.severity == "error"]


class ArtifactValidator:
    def validate_required_outputs(self, output_dir: Path, required_outputs: list[str]) -> tuple[bool, list[str]]:
        result = self.validate_required_outputs_result(output_dir, required_outputs)
        return result.ok, result.errors

    def validate_required_outputs_result(self, output_dir: Path, required_outputs: list[str]) -> ValidationResult:
        issues: list[ValidationIssue] = []
        metadata = load_artifact_metadata(output_dir)
        for relative_name in required_outputs:
            path = output_dir / relative_name
            if not path.exists():
                issues.append(
                    ValidationIssue(relative_name, "missing_required_output", f"Missing required output: {relative_name}")
                )
            elif not path.is_file():
                issues.append(
                    ValidationIssue(relative_name, "required_output_not_file", f"Required output is not a file: {relative_name}")
                )
            elif path.stat().st_size == 0:
                issues.append(
                    ValidationIssue(relative_name, "required_output_empty", f"Required output is empty: {relative_name}")
                )
            elif output_has_pending_template_marker(path):
                issues.append(
                    ValidationIssue(
                        relative_name,
                        "template_not_completed",
                        f"{relative_name} still contains Harness output template marker",
                    )
                )
            elif relative_name == REVIEW_RESULT_ARTIFACT:
                issues.extend(self.validate_review_result(path))
            elif relative_name == PEER_REVIEW_RESULT_ARTIFACT:
                issues.extend(self.validate_peer_review_result(path))
            elif relative_name in {
                "decision.json",
                "todo_breakdown.json",
                "selected_plan.json",
                "tester_result.json",
                "merged_patch_metadata.json",
                "final_delivery.json",
            }:
                issues.extend(self.validate_json_artifact(path, relative_name))
        delivery_path = output_dir / "delivery.md"
        if delivery_path.exists() and delivery_path.is_file():
            return_code = metadata_int_field(metadata, "delivery.md", "return_code")
            if return_code is None:
                return_code = self.parse_delivery_return_code(delivery_path)
            if return_code is None:
                issues.append(
                    ValidationIssue("delivery.md", "missing_return_code", "delivery.md must contain `return_code: <int>`")
                )
            elif return_code != DELIVERY_SUCCESS_RETURN_CODE:
                issues.append(
                    ValidationIssue(
                        "delivery.md",
                        "nonzero_return_code",
                        f"delivery.md reports non-zero return_code: {return_code}",
                    )
                )
        for relative_name in required_outputs:
            if relative_name == "delivery.md" or not relative_name.endswith(".md"):
                continue
            path = output_dir / relative_name
            if not path.exists() or not path.is_file() or path.stat().st_size == 0:
                continue
            artifact_code = metadata_int_field(metadata, relative_name, "artifact_result_code")
            if artifact_code is None:
                artifact_code = self.parse_markdown_artifact_result_code(path)
            if artifact_code is None:
                issues.append(
                    ValidationIssue(
                        relative_name,
                        "missing_artifact_result_code",
                        f"{relative_name} must contain `artifact_result_code: <int>`",
                    )
                )
            elif artifact_code != DELIVERY_SUCCESS_RETURN_CODE:
                issues.append(
                    ValidationIssue(
                        relative_name,
                        "nonzero_artifact_result_code",
                        f"{relative_name} reports non-zero artifact_result_code: {artifact_code}",
                    )
                )
        return ValidationResult(tuple(issues))

    def validate_review_result(self, path: Path) -> list[ValidationIssue]:
        text = path.read_text(encoding="utf-8", errors="replace")
        payload = parse_review_result_content(text)
        if not payload:
            return [
                ValidationIssue(
                    REVIEW_RESULT_ARTIFACT,
                    "invalid_review_result_json",
                    "review_result.json must be one JSON object",
                )
            ]
        return [
            ValidationIssue(REVIEW_RESULT_ARTIFACT, "invalid_review_result_schema", message)
            for message in validate_review_result_payload(payload)
        ]

    def validate_peer_review_result(self, path: Path) -> list[ValidationIssue]:
        text = path.read_text(encoding="utf-8", errors="replace")
        payload = parse_peer_review_result_content(text)
        if not payload:
            return [
                ValidationIssue(
                    PEER_REVIEW_RESULT_ARTIFACT,
                    "invalid_peer_review_result_json",
                    "peer_review_result.json must be one JSON object",
                )
            ]
        return [
            ValidationIssue(PEER_REVIEW_RESULT_ARTIFACT, "invalid_peer_review_result_schema", message)
            for message in validate_peer_review_result_payload(payload)
        ]

    def validate_json_artifact(self, path: Path, artifact_name: str) -> list[ValidationIssue]:
        payload = self.load_json_object(path)
        if payload is None:
            return [
                ValidationIssue(
                    artifact_name,
                    "invalid_json_artifact",
                    f"{artifact_name} must be one JSON object",
                )
            ]
        messages: list[str] = []
        if artifact_name == "decision.json":
            messages.extend(self.validate_decision_json(payload))
        elif artifact_name == "todo_breakdown.json":
            if not isinstance(payload.get("todos"), list):
                messages.append("todo_breakdown.json.todos must be a list")
        elif artifact_name == "selected_plan.json":
            if not isinstance(payload.get("summary"), str) or not payload.get("summary", "").strip():
                messages.append("selected_plan.json.summary must be a non-empty string")
            messages.extend(validate_acceptance_oracles(payload))
        elif artifact_name == "tester_result.json":
            messages.extend(validate_tester_oracle_results(payload))
        elif artifact_name == "merged_patch_metadata.json":
            if payload.get("patch_artifact") != "merged_patch.diff":
                messages.append('merged_patch_metadata.json.patch_artifact must be "merged_patch.diff"')
            if not isinstance(payload.get("changed_files"), list):
                messages.append("merged_patch_metadata.json.changed_files must be a list")
            if not isinstance(payload.get("merge_report"), dict):
                messages.append("merged_patch_metadata.json.merge_report must be an object")
        elif artifact_name == "final_delivery.json":
            code = payload.get("final_delivery_code")
            if isinstance(code, bool):
                code = None
            elif isinstance(code, str):
                try:
                    code = int(code.strip())
                except ValueError:
                    code = None
            if code not in {-1, 0, 1, 2}:
                messages.append("final_delivery.json.final_delivery_code must be one of -1, 0, 1, or 2")
            if not isinstance(payload.get("summary"), str) or not payload.get("summary", "").strip():
                messages.append("final_delivery.json.summary must be a non-empty string")
        return [ValidationIssue(artifact_name, "invalid_json_artifact_schema", message) for message in messages]

    def validate_decision_json(self, payload: dict[str, object]) -> list[str]:
        messages: list[str] = []
        code = payload.get("decision_code")
        if isinstance(code, bool):
            code = None
        elif isinstance(code, str):
            try:
                code = int(code.strip())
            except ValueError:
                code = None
        if code not in {-1, 0, 1}:
            messages.append("decision.json.decision_code must be one of -1, 0, or 1")
        if not isinstance(payload.get("decision"), str) or not payload.get("decision", "").strip():
            messages.append("decision.json.decision must be a non-empty string")
        if not isinstance(payload.get("summary"), str) or not payload.get("summary", "").strip():
            messages.append("decision.json.summary must be a non-empty string")
        if not isinstance(payload.get("reason"), str) or not payload.get("reason", "").strip():
            messages.append("decision.json.reason must be a non-empty string")
        if "evidence" not in payload:
            messages.append("decision.json.evidence is required")
        return messages

    def load_json_object(self, path: Path) -> dict[str, object] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def repair_trivial_contract_issues(self, output_dir: Path, validation_result: ValidationResult) -> list[str]:
        repaired: list[str] = []
        for issue in validation_result.issues:
            if issue.severity == "error" and issue.code != "missing_artifact_result_code":
                continue
            path = output_dir / issue.artifact
            if not path.exists() or not path.is_file() or path.stat().st_size == 0:
                continue
            if output_has_pending_template_marker(path):
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if self.parse_markdown_artifact_result_code(path) is not None:
                continue
            path.write_text(f"artifact_result_code: {DELIVERY_SUCCESS_RETURN_CODE}\n\n{content}", encoding="utf-8")
            repaired.append(issue.artifact)
        return repaired

    def parse_delivery_return_code(self, delivery_path: Path) -> int | None:
        if not delivery_path.exists() or not delivery_path.is_file():
            return None
        content = delivery_path.read_text(encoding="utf-8", errors="replace")
        json_return_code = self.parse_delivery_json_return_code(content)
        if json_return_code is not None:
            return json_return_code
        for line in content.splitlines():
            match = RETURN_CODE_FIELD_PATTERN.fullmatch(line.strip())
            if match:
                return int(match.group(1))
        return None

    def parse_delivery_json_return_code(self, content: str) -> int | None:
        text = content.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get("return_code")
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None

    def parse_markdown_artifact_result_code(self, artifact_path: Path) -> int | None:
        if not artifact_path.exists() or not artifact_path.is_file():
            return None
        for line in artifact_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = ARTIFACT_RESULT_CODE_FIELD_PATTERN.fullmatch(line.strip())
            if match:
                return int(match.group(1))
        return None

    def parse_delivery_status(self, delivery_path: Path) -> str | None:
        return_code = self.parse_delivery_return_code(delivery_path)
        if return_code is None:
            return None
        return delivery_status_for_return_code(return_code)


def delivery_issue_is_contract_only(result: ValidationResult) -> bool:
    errors = [issue for issue in result.issues if issue.severity == "error"]
    if not errors:
        return False
    delivery_contract_codes = {"missing_return_code", "nonzero_return_code", "template_not_completed"}
    return all(issue.artifact == "delivery.md" and issue.code in delivery_contract_codes for issue in errors)
