from __future__ import annotations

from pathlib import Path


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
        },
        "agent_backend": {
            "default": "mock",
            "planner": "mock",
            "executor": "mock",
            "tester": "mock",
            "reviewer": "mock",
            "judge": "mock",
            "communicator": "mock",
        },
        "roles": {
            "planner": {"count": 2},
            "executor": {"count": 2},
            "tester": {"count": 2},
            "reviewer": {"count": 2},
            "judge": {"count": 1},
            "communicator": {"count": 1},
        },
        "limits": {
            "max_planning_rounds": 3,
            "max_test_fix_rounds": 5,
            "max_review_rounds": 3,
            "max_agent_retry": 2,
        },
        "timeouts": {
            "planner": 5,
            "executor": 5,
            "tester": 5,
            "reviewer": 5,
            "judge": 5,
            "communicator": 5,
        },
        "testing": {
            "runtime": "native",
        },
        "policy": {
            "different_roles_can_run_concurrently": False,
            "same_role_can_run_concurrently": True,
            "allow_medium_bug_delivery": False,
            "require_all_tests_pass": True,
        },
    }
