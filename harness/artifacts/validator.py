from __future__ import annotations

from pathlib import Path
import re

from harness.artifacts.delivery_codes import DELIVERY_SUCCESS_RETURN_CODE, delivery_status_for_return_code

RETURN_CODE_FIELD_PATTERN = re.compile(r"^return_code\s*:\s*(-?\d+)\s*$")
ARTIFACT_RESULT_CODE_FIELD_PATTERN = re.compile(r"^artifact_result_code\s*:\s*(-?\d+)\s*$")


class ArtifactValidator:
    def validate_required_outputs(self, output_dir: Path, required_outputs: list[str]) -> tuple[bool, list[str]]:
        errors: list[str] = []
        for relative_name in required_outputs:
            path = output_dir / relative_name
            if not path.exists():
                errors.append(f"Missing required output: {relative_name}")
            elif not path.is_file():
                errors.append(f"Required output is not a file: {relative_name}")
            elif path.stat().st_size == 0:
                errors.append(f"Required output is empty: {relative_name}")
        delivery_path = output_dir / "delivery.md"
        if delivery_path.exists() and delivery_path.is_file():
            return_code = self.parse_delivery_return_code(delivery_path)
            if return_code is None:
                errors.append("delivery.md must contain `return_code: <int>`")
            elif return_code != DELIVERY_SUCCESS_RETURN_CODE:
                errors.append(f"delivery.md reports non-zero return_code: {return_code}")
        for relative_name in required_outputs:
            if relative_name == "delivery.md" or not relative_name.endswith(".md"):
                continue
            path = output_dir / relative_name
            if not path.exists() or not path.is_file() or path.stat().st_size == 0:
                continue
            artifact_code = self.parse_markdown_artifact_result_code(path)
            if artifact_code is None:
                errors.append(
                    f"{relative_name} must contain `artifact_result_code: <int>`"
                )
            elif artifact_code != DELIVERY_SUCCESS_RETURN_CODE:
                errors.append(f"{relative_name} reports non-zero artifact_result_code: {artifact_code}")
        return not errors, errors

    def parse_delivery_return_code(self, delivery_path: Path) -> int | None:
        if not delivery_path.exists() or not delivery_path.is_file():
            return None
        for line in delivery_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = RETURN_CODE_FIELD_PATTERN.fullmatch(line.strip())
            if match:
                return int(match.group(1))
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
