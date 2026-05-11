from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from harness.core.orchestrator import Orchestrator
from harness.core.workflow_classifier import WorkflowClassifier
from harness.core.workflow_type import MISC, NEW_PROJECT
from harness.delivery.handoff import format_delivery_handoff, format_total_elapsed

REAL_BACKENDS = ("codex", "claude", "gemini", "qwen")


def resolve_real_backend(requested: str) -> str:
    if requested == "auto":
        for candidate in REAL_BACKENDS:
            if shutil.which(candidate):
                return candidate
        raise RuntimeError("No real agent CLI found. Install one of: codex, claude, gemini, qwen.")
    if not shutil.which(requested):
        raise RuntimeError(f"Requested backend `{requested}` was not found on PATH.")
    return requested


def run_once(
    orchestrator: Orchestrator,
    prompt: str,
    workflow_type: str = NEW_PROJECT,
    project_context_md: str | None = None,
) -> int:
    task_id = orchestrator.create_task(prompt, workflow_type=workflow_type)
    if project_context_md:
        orchestrator.attach_project_context(task_id, project_context_md)
    result_path = orchestrator.run_task(task_id, workflow_type=workflow_type)
    if workflow_type == MISC:
        print(f"response: {Path(result_path)}")
    else:
        usage_guide = orchestrator.communicator.latest_usage_guide(task_id)
        for line in format_delivery_handoff(Path(result_path), usage_guide):
            print(line)
        print(format_total_elapsed(orchestrator.repository.get_task(task_id)))
    return 0


def run_existing(
    orchestrator: Orchestrator,
    task_id: str,
    prompt: str,
    workflow_type: str = NEW_PROJECT,
    project_context_md: str | None = None,
) -> int:
    if project_context_md:
        orchestrator.attach_project_context(task_id, project_context_md)
    result_path = orchestrator.run_task(task_id, workflow_type=workflow_type, user_prompt_override=prompt)
    if workflow_type == MISC:
        print(f"response: {Path(result_path)}")
    else:
        usage_guide = orchestrator.communicator.latest_usage_guide(task_id)
        for line in format_delivery_handoff(Path(result_path), usage_guide):
            print(line)
        print(format_total_elapsed(orchestrator.repository.get_task(task_id)))
    return 0


def classify_workflow(prompt: str, backend: str, config: dict[str, Any] | None = None) -> tuple[str, str | None]:
    workflow_type, _log_dir, fallback_answer = WorkflowClassifier(backend, config=config).classify_with_fallback(prompt)
    return workflow_type, fallback_answer
