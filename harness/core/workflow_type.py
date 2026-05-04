from __future__ import annotations

BUGFIX = "bugfix"
FEATURE_CHANGE = "feature_change"
NEW_PROJECT = "new_project"
MISC = "misc"

WORKFLOW_TYPES = {BUGFIX, FEATURE_CHANGE, NEW_PROJECT, MISC}


def normalize_workflow_type(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "fix": BUGFIX,
        "bug": BUGFIX,
        "bug_fix": BUGFIX,
        "bugfix": BUGFIX,
        "repair": BUGFIX,
        "修复": BUGFIX,
        "feature": FEATURE_CHANGE,
        "change": FEATURE_CHANGE,
        "modify": FEATURE_CHANGE,
        "feature_change": FEATURE_CHANGE,
        "新增": FEATURE_CHANGE,
        "修改": FEATURE_CHANGE,
        "新增/修改功能": FEATURE_CHANGE,
        "new": NEW_PROJECT,
        "project": NEW_PROJECT,
        "new_project": NEW_PROJECT,
        "新工程": NEW_PROJECT,
        "新项目": NEW_PROJECT,
        "misc": MISC,
        "question": MISC,
        "ask": MISC,
        "chat": MISC,
        "qa": MISC,
        "咨询": MISC,
        "问答": MISC,
    }
    if normalized in aliases:
        return aliases[normalized]
    if normalized in WORKFLOW_TYPES:
        return normalized
    raise ValueError(f"Unsupported workflow type: {value}")
