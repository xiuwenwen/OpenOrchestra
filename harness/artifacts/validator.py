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
EMPTY_ALLOWED_REQUIRED_OUTPUTS = {"fix_patch.diff"}
CONTRACT_ARTIFACTS = {
    "environment_contract_draft.json",
    "environment_contract.json",
    "validation_contract_draft.json",
    "validation_contract.json",
}
PLAN_REVIEW_REQUIRED_ARTIFACTS = {
    REVIEW_RESULT_ARTIFACT,
    "selected_plan.json",
    "environment_contract.json",
    "validation_contract.json",
}
CONTRACT_MODES = {"explicit", "benchmark_spec", "repo_discovery", "agent_discovery", "none", "unknown"}
CONTRACT_CONFIDENCE_VALUES = {"high", "medium", "low", "unknown"}
CONTRACT_STATUSES = {"draft", "final"}
ORACLE_KIND_ALIASES = {
    "existing_test": "test",
    "regression": "test",
    "compile": "test",
    "unit_test": "test",
    "integration_test": "test",
    "pytest": "test",
    "command": "runtime",
    "smoke": "runtime",
    "runtime_check": "runtime",
    "code_inspection": "static",
}
TESTER_FAILURE_TYPES = {
    "none",
    "source_bug",
    "environment_bug",
    "env_setup",
    "test_command_bug",
    "test_command",
    "contract_bug",
    "process_bug",
    "test",
    "inconclusive",
}


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
            elif path.stat().st_size == 0 and not required_output_may_be_empty(relative_name):
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
                *CONTRACT_ARTIFACTS,
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
        if PLAN_REVIEW_REQUIRED_ARTIFACTS.issubset(set(required_outputs)):
            issues.extend(self.validate_plan_review_handoff(output_dir))
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
            for field in ("risks", "required_executor_notes", "reviewer_integrated_findings"):
                if field in payload and not isinstance(payload.get(field), list):
                    messages.append(f"selected_plan.json.{field} must be a list when present")
            messages.extend(validate_acceptance_oracles(payload))
        elif artifact_name == "tester_result.json":
            messages.extend(self.validate_tester_result_json(payload))
            messages.extend(validate_tester_oracle_results(payload))
        elif artifact_name in CONTRACT_ARTIFACTS:
            messages.extend(self.validate_contract_json(payload, artifact_name))
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

    def validate_plan_review_handoff(self, output_dir: Path) -> list[ValidationIssue]:
        review_payload = self.load_json_object(output_dir / REVIEW_RESULT_ARTIFACT)
        selected_plan = self.load_json_object(output_dir / "selected_plan.json")
        if review_payload is None or selected_plan is None:
            return []

        if self.coerce_int(review_payload.get("review_decision_code")) != 0:
            return []

        messages: list[str] = []
        if self._has_non_empty_list(review_payload.get("required_changes")):
            messages.append(
                "PLAN_REVIEW review_decision_code 0 requires review_result.json.required_changes to be empty; "
                "put non-blocking notes into selected_plan.json instead"
            )

        has_non_blocking_review_notes = (
            self._has_non_empty_list(review_payload.get("findings"))
            or self._has_non_empty_list(review_payload.get("acceptance_oracle_changes"))
        )
        has_selected_plan_carry_forward = (
            self._has_non_empty_list(selected_plan.get("reviewer_integrated_findings"))
            or self._has_non_empty_list(selected_plan.get("required_executor_notes"))
            or self._has_non_empty_list(selected_plan.get("risks"))
        )
        if has_non_blocking_review_notes and not has_selected_plan_carry_forward:
            messages.append(
                "PLAN_REVIEW review_decision_code 0 with reviewer findings requires selected_plan.json to carry them "
                "forward in reviewer_integrated_findings, required_executor_notes, or risks"
            )

        return [
            ValidationIssue("selected_plan.json", "plan_review_findings_not_integrated", message)
            for message in messages
        ]

    def validate_contract_json(self, payload: dict[str, object], artifact_name: str) -> list[str]:
        messages: list[str] = []
        is_environment = artifact_name.startswith("environment_contract")
        is_draft = artifact_name.endswith("_draft.json")
        expected_schema = "environment_contract.v1" if is_environment else "validation_contract.v1"
        expected_status = "draft" if is_draft else "final"

        if payload.get("schema_version") != expected_schema:
            messages.append(f"{artifact_name}.schema_version must be {expected_schema!r}")
        if not self._non_empty_string(payload.get("contract_id")):
            messages.append(f"{artifact_name}.contract_id must be a non-empty string")
        status = self._normalized_string(payload.get("contract_status"))
        if status not in CONTRACT_STATUSES:
            messages.append(f"{artifact_name}.contract_status must be one of: draft, final")
        elif status != expected_status:
            messages.append(f"{artifact_name}.contract_status must be {expected_status!r}")
        confidence = self._normalized_string(payload.get("confidence"))
        if confidence not in CONTRACT_CONFIDENCE_VALUES:
            messages.append(f"{artifact_name}.confidence must be one of: high, medium, low, unknown")
        for field in ("unknowns", "evidence_sources"):
            if not isinstance(payload.get(field), list):
                messages.append(f"{artifact_name}.{field} must be a list")

        if is_environment:
            runtime = payload.get("runtime")
            if not isinstance(runtime, dict):
                messages.append(f"{artifact_name}.runtime must be an object")
            elif not self._non_empty_string(runtime.get("type")):
                messages.append(f"{artifact_name}.runtime.type must be a non-empty string")
            setup = payload.get("setup")
            if not isinstance(setup, dict):
                messages.append(f"{artifact_name}.setup must be an object")
            else:
                messages.extend(self.validate_command_section(setup, f"{artifact_name}.setup"))
            dependencies = payload.get("dependencies")
            if dependencies is not None:
                if not isinstance(dependencies, dict):
                    messages.append(f"{artifact_name}.dependencies must be an object")
                else:
                    messages.extend(self.validate_command_section(dependencies, f"{artifact_name}.dependencies"))
            constraints = payload.get("constraints")
            if constraints is not None:
                if not isinstance(constraints, dict):
                    messages.append(f"{artifact_name}.constraints must be an object")
                elif not isinstance(constraints.get("forbidden_validation_methods", []), list):
                    messages.append(f"{artifact_name}.constraints.forbidden_validation_methods must be a list")
        else:
            tests = payload.get("tests")
            if not isinstance(tests, dict):
                messages.append(f"{artifact_name}.tests must be an object")
            else:
                messages.extend(self.validate_command_section(tests, f"{artifact_name}.tests"))
                for field in ("fail_to_pass", "pass_to_pass"):
                    if field in tests and not isinstance(tests.get(field), list):
                        messages.append(f"{artifact_name}.tests.{field} must be a list")
            pass_criteria = payload.get("pass_criteria")
            if not isinstance(pass_criteria, dict):
                messages.append(f"{artifact_name}.pass_criteria must be an object")
            elif not self._non_empty_string(pass_criteria.get("type")):
                messages.append(f"{artifact_name}.pass_criteria.type must be a non-empty string")
            if not isinstance(payload.get("acceptance_oracle_ids", []), list):
                messages.append(f"{artifact_name}.acceptance_oracle_ids must be a list")

        return messages

    def validate_command_section(self, section: dict[str, object], prefix: str) -> list[str]:
        messages: list[str] = []
        mode = self._normalized_string(section.get("mode"))
        if mode not in CONTRACT_MODES:
            messages.append(f"{prefix}.mode must be one of: {', '.join(sorted(CONTRACT_MODES))}")
        commands = section.get("commands")
        if not isinstance(commands, list):
            messages.append(f"{prefix}.commands must be a list")
        elif not all(isinstance(command, str) for command in commands):
            messages.append(f"{prefix}.commands must contain only strings")
        elif mode == "explicit" and not any(command.strip() for command in commands):
            messages.append(f"{prefix}.commands must be non-empty when mode is explicit")
        discovery_allowed = section.get("discovery_allowed")
        if discovery_allowed is not None and not isinstance(discovery_allowed, bool):
            messages.append(f"{prefix}.discovery_allowed must be a boolean when present")
        return messages

    def validate_tester_result_json(self, payload: dict[str, object]) -> list[str]:
        messages: list[str] = []
        failure_type = self._normalized_string(payload.get("failure_type"))
        if failure_type and failure_type not in TESTER_FAILURE_TYPES:
            messages.append(
                "tester_result.json.failure_type must be one of: "
                + ", ".join(sorted(TESTER_FAILURE_TYPES))
            )
        if "environment_ready" in payload and not isinstance(payload.get("environment_ready"), bool):
            messages.append("tester_result.json.environment_ready must be a boolean when present")
        return messages

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

    def _normalized_string(self, value: object) -> str:
        return value.strip().lower() if isinstance(value, str) else ""

    def _non_empty_string(self, value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def _has_non_empty_list(self, value: object) -> bool:
        return isinstance(value, list) and any(bool(str(item).strip()) for item in value)

    def load_json_object(self, path: Path) -> dict[str, object] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def repair_trivial_contract_issues(self, output_dir: Path, validation_result: ValidationResult) -> list[str]:
        repaired = self.repair_trivial_json_enum_aliases(output_dir, validation_result)
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

    def repair_trivial_json_enum_aliases(self, output_dir: Path, validation_result: ValidationResult) -> list[str]:
        repaired: list[str] = []
        if any(issue.artifact == "selected_plan.json" and ".kind must be one of" in issue.message for issue in validation_result.issues):
            if self.repair_selected_plan_oracle_kind_aliases(output_dir / "selected_plan.json"):
                repaired.append("selected_plan.json")
        return repaired

    def repair_selected_plan_oracle_kind_aliases(self, path: Path) -> bool:
        payload = self.load_json_object(path)
        if payload is None or output_has_pending_template_marker(path):
            return False
        oracles = payload.get("acceptance_oracles")
        if not isinstance(oracles, list):
            return False
        notes: list[str] = []
        for index, oracle in enumerate(oracles):
            if not isinstance(oracle, dict):
                continue
            kind = self._normalized_string(oracle.get("kind"))
            canonical = ORACLE_KIND_ALIASES.get(kind)
            if canonical and canonical != kind:
                oracle["kind"] = canonical
                notes.append(f"acceptance_oracles[{index}].kind {kind!r} -> {canonical!r}")
        if not notes:
            return False
        for note in notes:
            self.add_canonicalization_note(payload, note)
        self.write_json_object(path, payload)
        return True

    def add_canonicalization_note(self, payload: dict[str, object], note: str) -> None:
        notes = payload.get("harness_canonicalizations")
        if not isinstance(notes, list):
            notes = []
            payload["harness_canonicalizations"] = notes
        notes.append(note)

    def write_json_object(self, path: Path, payload: dict[str, object]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def coerce_int(self, value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

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


def required_output_may_be_empty(relative_name: str) -> bool:
    return relative_name in EMPTY_ALLOWED_REQUIRED_OUTPUTS
