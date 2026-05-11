from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from harness.artifacts.delivery_codes import DELIVERY_SUCCESS_RETURN_CODE, delivery_status_for_return_code
from harness.artifacts.output_templates import output_has_pending_template_marker

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
        delivery_path = output_dir / "delivery.md"
        if delivery_path.exists() and delivery_path.is_file():
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
