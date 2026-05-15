from __future__ import annotations

import json
from typing import Any, Mapping

from harness.core.workflow_type import MISC, normalize_workflow_type

DEFAULT_PLANNER_PEER_REVIEW_DIFFICULTY_THRESHOLD = 5


def planner_peer_review_difficulty_threshold(config: Mapping[str, Any] | None) -> int:
    limits = (config or {}).get("limits", {})
    if not isinstance(limits, Mapping):
        return DEFAULT_PLANNER_PEER_REVIEW_DIFFICULTY_THRESHOLD
    value = limits.get(
        "planner_peer_review_difficulty_threshold",
        DEFAULT_PLANNER_PEER_REVIEW_DIFFICULTY_THRESHOLD,
    )
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_PLANNER_PEER_REVIEW_DIFFICULTY_THRESHOLD


def planner_peer_review_enabled_for_score(
    config: Mapping[str, Any] | None,
    workflow_type: str,
    difficulty_score: int | None,
) -> bool:
    if normalize_workflow_type(workflow_type) == MISC or difficulty_score is None:
        return False
    return difficulty_score > planner_peer_review_difficulty_threshold(config)


def classification_from_task_configuration(configuration: str | None) -> dict[str, Any]:
    if not configuration:
        return {}
    try:
        payload = json.loads(configuration)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    classification = payload.get("classification")
    return classification if isinstance(classification, dict) else {}


def difficulty_score_from_task_configuration(configuration: str | None) -> int | None:
    classification = classification_from_task_configuration(configuration)
    value = classification.get("difficulty_score")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
