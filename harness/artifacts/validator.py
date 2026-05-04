from __future__ import annotations

from pathlib import Path
import re

STATUS_FIELD_PATTERN = re.compile(r"^status\s*:\s*(success|failed|partial)\s*$", re.IGNORECASE)


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
            status = self.parse_delivery_status(delivery_path)
            if status is None:
                errors.append("delivery.md must contain `status: success|failed|partial`")
            elif status != "success":
                errors.append(f"delivery.md reports non-success status: {status}")
        return not errors, errors

    def parse_delivery_status(self, delivery_path: Path) -> str | None:
        if not delivery_path.exists() or not delivery_path.is_file():
            return None
        for line in delivery_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = STATUS_FIELD_PATTERN.match(self._normalize_markdown_field_line(line))
            if match:
                return match.group(1).lower()
        return None

    def _normalize_markdown_field_line(self, line: str) -> str:
        normalized = line.strip().replace("：", ":")
        normalized = re.sub(r"^#{1,6}\s+", "", normalized).strip()
        normalized = re.sub(r"^[-*+]\s+", "", normalized).strip()
        normalized = re.sub(r"\*\*(.*?)\*\*", r"\1", normalized)
        normalized = re.sub(r"__(.*?)__", r"\1", normalized)
        normalized = normalized.strip("`").strip()
        return normalized
