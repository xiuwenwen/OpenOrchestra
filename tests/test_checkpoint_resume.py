from __future__ import annotations

import json
import sys
import uuid
import re
from pathlib import Path
from concurrent.futures import wait as real_wait

import pytest

from harness.agents import runner as agent_runner_module
from harness.agents.result import AgentRunResult, ArtifactRef
from harness.artifacts.schemas import required_outputs_for
import harness.core.orchestrator as orchestrator_module
from harness.core.orchestrator import Orchestrator
from harness.core.progress import ProgressEvent
from harness.core.state_machine import (
    DELIVERY,
    EXECUTION,
    FAILED,
    FIXING,
    PATCH_MERGE,
    PLAN_REVIEW,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEWING,
    RUNNING,
    TESTING,
)
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, NEW_PROJECT
from harness.patch.gate import materialized_repo_markdown, run_patch_gate


from orchestrator_mock_support import _config


def _valid_checkpoint_content(artifact_type: str) -> str:
    if artifact_type == "delivery.md":
        return "return_code: 0\n"
    if artifact_type == "todo_breakdown.json":
        return json.dumps({"schema_version": 1, "todos": [], "risks": []}) + "\n"
    if artifact_type in {"environment_contract_draft.json", "environment_contract.json"}:
        return json.dumps(
            {
                "schema_version": "environment_contract.v1",
                "contract_id": "env",
                "contract_status": "draft" if artifact_type.endswith("_draft.json") else "final",
                "source": "checkpoint",
                "confidence": "unknown",
                "runtime": {"type": "unknown"},
                "setup": {"mode": "unknown", "commands": [], "discovery_allowed": True},
                "dependencies": {"mode": "unknown", "commands": [], "files": []},
                "unknowns": ["checkpoint fixture"],
                "evidence_sources": [],
            }
        ) + "\n"
    if artifact_type in {"validation_contract_draft.json", "validation_contract.json"}:
        return json.dumps(
            {
                "schema_version": "validation_contract.v1",
                "contract_id": "validation",
                "contract_status": "draft" if artifact_type.endswith("_draft.json") else "final",
                "source": "checkpoint",
                "confidence": "unknown",
                "runtime": "unknown",
                "tests": {"mode": "unknown", "commands": [], "discovery_allowed": True},
                "pass_criteria": {"type": "unknown", "conditions": []},
                "acceptance_oracle_ids": [],
                "unknowns": ["checkpoint fixture"],
                "evidence_sources": [],
            }
        ) + "\n"
    return f"artifact_result_code: 0\n\n# {artifact_type}\n"


def test_failed_phase_with_completed_agent_runs_is_recovered_on_resume(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("recover old concurrent planner phase")
    phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    for agent_id in ("planner-1", "planner-2"):
        run_id = orchestrator.repository.create_agent_run(task_id, phase_id, "planner", agent_id, 0)
        output_dir = tmp_path / f"{agent_id}-output"
        output_dir.mkdir()
        for artifact_type in required_outputs_for("planner", "PLANNING_DRAFT"):
            path = output_dir / artifact_type
            path.write_text(_valid_checkpoint_content(artifact_type), encoding="utf-8")
            orchestrator.repository.create_artifact(
                ArtifactRef(
                    artifact_id=str(uuid.uuid4()),
                    task_id=task_id,
                    phase_id=phase_id,
                    role="planner",
                    agent_id=agent_id,
                    artifact_type=artifact_type,
                    path=path,
                    version=1,
                    hash="hash",
                )
            )
        orchestrator.repository.update_agent_run_status(run_id, "COMPLETED")
    orchestrator.repository.update_phase_status(phase_id, "FAILED")

    results = orchestrator.run_role_phase(
        "planner",
        PLANNING_DRAFT,
        0,
        required_outputs_for("planner", PLANNING_DRAFT),
        "recover old concurrent planner phase",
    )

    assert {result.agent_id for result in results} == {"planner-1", "planner-2"}
    assert orchestrator.repository.list_phases(task_id)[0]["status"] == "COMPLETED"

def test_failed_phase_is_not_recovered_when_required_artifacts_are_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("rerun incomplete old phase")
    phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    run_id = orchestrator.repository.create_agent_run(task_id, phase_id, "planner", "planner-1", 0)
    delivery = tmp_path / "delivery.md"
    delivery.write_text("return_code: 0\n", encoding="utf-8")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=phase_id,
            role="planner",
            agent_id="planner-1",
            artifact_type="delivery.md",
            path=delivery,
            version=1,
            hash="hash",
        )
    )
    orchestrator.repository.update_agent_run_status(run_id, "COMPLETED")
    orchestrator.repository.update_phase_status(phase_id, "FAILED")

    orchestrator.run_role_phase(
        "planner",
        PLANNING_DRAFT,
        0,
        required_outputs_for("planner", PLANNING_DRAFT),
        "rerun incomplete old phase",
    )

    phases = orchestrator.repository.list_phases(task_id)
    assert len(phases) == 2
    assert phases[-1]["status"] == "COMPLETED"

def test_checkpoint_resume_prefers_latest_recoverable_phase(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("prefer latest checkpoint")
    old_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    old_run_id = orchestrator.repository.create_agent_run(task_id, old_phase_id, "planner", "planner-1", 0)
    old_output_dir = tmp_path / "old-output"
    old_output_dir.mkdir()
    old_delivery = old_output_dir / "delivery.md"
    old_delivery.write_text("return_code: 0\n", encoding="utf-8")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=old_phase_id,
            role="planner",
            agent_id="planner-1",
            artifact_type="delivery.md",
            path=old_delivery,
            version=1,
            hash="hash",
        )
    )
    orchestrator.repository.update_agent_run_status(old_run_id, "COMPLETED")
    orchestrator.repository.update_phase_status(old_phase_id, "FAILED")

    latest_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    latest_run_id = orchestrator.repository.create_agent_run(task_id, latest_phase_id, "planner", "planner-1", 0)
    latest_output_dir = tmp_path / "latest-output"
    latest_output_dir.mkdir()
    for artifact_type in required_outputs_for("planner", PLANNING_DRAFT):
        path = latest_output_dir / artifact_type
        path.write_text(_valid_checkpoint_content(artifact_type), encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=latest_phase_id,
                role="planner",
                agent_id="planner-1",
                artifact_type=artifact_type,
                path=path,
                version=2,
                hash="hash",
            )
        )
    orchestrator.repository.update_agent_run_status(latest_run_id, "COMPLETED")
    orchestrator.repository.update_phase_status(latest_phase_id, "COMPLETED")

    results = orchestrator.run_role_phase(
        "planner",
        PLANNING_DRAFT,
        0,
        required_outputs_for("planner", PLANNING_DRAFT),
        "prefer latest checkpoint",
    )

    assert [result.phase_id for result in results] == [latest_phase_id]


def test_checkpoint_resume_ignores_previous_prompt_turn(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("Build a weather app")
    old_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    old_run_id = orchestrator.repository.create_agent_run(task_id, old_phase_id, "planner", "planner-1", 0)
    output_dir = tmp_path / "old-output"
    output_dir.mkdir()
    for artifact_type in required_outputs_for("planner", PLANNING_DRAFT):
        path = output_dir / artifact_type
        path.write_text(_valid_checkpoint_content(artifact_type), encoding="utf-8")
        orchestrator.repository.create_artifact(ArtifactRef(str(uuid.uuid4()), task_id, old_phase_id, "planner", "planner-1", artifact_type, path, 1, "hash"))
    orchestrator.repository.update_agent_run_status(old_run_id, "COMPLETED")
    orchestrator.repository.update_phase_status(old_phase_id, "COMPLETED")
    orchestrator.repository.append_task_prompt_turn(task_id, "Add export")

    results = orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, required_outputs_for("planner", PLANNING_DRAFT), "Add export")

    assert [result.phase_id for result in results] != [old_phase_id]
    assert orchestrator.repository.list_phases(task_id)[-1]["prompt_turn_id"] == 1


def test_workflow_round_helpers_ignore_previous_prompt_turn(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("Build a weather app")
    orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0, status="COMPLETED")
    orchestrator.repository.create_phase(task_id, FIXING, "executor", 7, status="COMPLETED")
    orchestrator.repository.append_task_prompt_turn(task_id, "Add export")

    assert orchestrator.workflow_engine.next_phase_round_id(task_id) == 0
    assert orchestrator.workflow_engine.highest_bugfix_round_id(task_id) is None
    assert orchestrator.workflow_engine.bugfix_needs_initial_planning(task_id, 0)


def test_checkpoint_resume_rejects_invalid_recovered_contract(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("reject invalid checkpoint")
    phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    run_id = orchestrator.repository.create_agent_run(task_id, phase_id, "planner", "planner-1", 0)
    output_dir = tmp_path / "invalid-output"
    output_dir.mkdir()
    for artifact_type in required_outputs_for("planner", PLANNING_DRAFT):
        path = output_dir / artifact_type
        path.write_text(
            "return_code: 0\n" if artifact_type == "delivery.md" else f"# {artifact_type}\n",
            encoding="utf-8",
        )
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role="planner",
                agent_id="planner-1",
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )
    orchestrator.repository.update_agent_run_status(run_id, "COMPLETED")
    orchestrator.repository.update_phase_status(phase_id, "COMPLETED")

    results = orchestrator.run_role_phase(
        "planner",
        PLANNING_DRAFT,
        0,
        required_outputs_for("planner", PLANNING_DRAFT),
        "rerun invalid checkpoint",
    )

    assert [result.phase_id for result in results] != [phase_id]
