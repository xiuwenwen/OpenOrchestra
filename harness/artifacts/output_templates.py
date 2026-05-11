from __future__ import annotations

import json
import re
from pathlib import Path


TEMPLATE_STATUS_FIELD = "harness_template_status"
TEMPLATE_PENDING_VALUE = "pending_model_completion"
TEMPLATE_PENDING_LINE = f"{TEMPLATE_STATUS_FIELD}: {TEMPLATE_PENDING_VALUE}"
TEMPLATE_PENDING_LINE_PATTERN = re.compile(
    rf"^\s*{re.escape(TEMPLATE_STATUS_FIELD)}\s*:\s*{re.escape(TEMPLATE_PENDING_VALUE)}\s*$",
    re.MULTILINE,
)


def seed_output_templates(
    output_dir: Path,
    required_outputs: list[str],
    *,
    role: str,
    phase: str,
    agent_id: str,
) -> list[Path]:
    """Create editable output templates for non-diff required files.

    The templates intentionally contain a pending marker. A model must replace
    the body and remove that marker before validation can pass.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    seeded: list[Path] = []
    for relative_name in required_outputs:
        if relative_name.endswith(".diff"):
            continue
        path = output_dir / relative_name
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            output_template_content(relative_name, required_outputs, role=role, phase=phase, agent_id=agent_id),
            encoding="utf-8",
        )
        seeded.append(path)
    return seeded


def output_template_content(
    relative_name: str,
    required_outputs: list[str],
    *,
    role: str,
    phase: str,
    agent_id: str,
) -> str:
    if relative_name == "delivery.md":
        return _delivery_template(required_outputs, role=role, phase=phase, agent_id=agent_id)
    if relative_name == "decision.json":
        return _decision_template(role=role, phase=phase, agent_id=agent_id)
    if relative_name.endswith(".md"):
        return _markdown_template(relative_name, role=role, phase=phase, agent_id=agent_id)
    return f"{TEMPLATE_PENDING_LINE}\n\nReplace this Harness output template with `{relative_name}` content.\n"


def output_has_pending_template_marker(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.name.endswith(".json") or path.name == "delivery.md":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if _json_contains_pending_template_value(payload):
            return True
    return bool(TEMPLATE_PENDING_LINE_PATTERN.search(text))


def _delivery_template(required_outputs: list[str], *, role: str, phase: str, agent_id: str) -> str:
    return (
        json.dumps(
            {
                "return_code": 0,
                "task_status": "success",
                "role_return_code": 0,
                "role": role,
                "phase": phase,
                "agent_id": agent_id,
                "produced_files": list(required_outputs),
                "known_risks": [],
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _decision_template(*, role: str, phase: str, agent_id: str) -> str:
    return (
        json.dumps(
            {
                "decision": TEMPLATE_PENDING_VALUE,
                "evidence": {},
                "reason": "Replace this Harness output template with the actual judge decision.",
                "role": role,
                "phase": phase,
                "agent_id": agent_id,
                TEMPLATE_STATUS_FIELD: TEMPLATE_PENDING_VALUE,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _json_contains_pending_template_value(value: object) -> bool:
    if value == TEMPLATE_PENDING_VALUE:
        return True
    if isinstance(value, dict):
        return any(_json_contains_pending_template_value(child) for child in value.values())
    if isinstance(value, list):
        return any(_json_contains_pending_template_value(child) for child in value)
    return False


def _markdown_template(relative_name: str, *, role: str, phase: str, agent_id: str) -> str:
    title = relative_name.removesuffix(".md").replace("_", " ").title()
    return (
        "artifact_result_code: 0\n"
        f"{TEMPLATE_PENDING_LINE}\n\n"
        f"# {title}\n\n"
        f"Harness pre-created this template for `{role}` `{phase}` `{agent_id}`.\n\n"
        "Replace this text with the completed deliverable content, keep the exact "
        "`artifact_result_code: 0` line, and remove the `harness_template_status` "
        "line before exiting.\n"
    )
