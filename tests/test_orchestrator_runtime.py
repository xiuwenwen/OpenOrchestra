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
    FINAL_JUDGEMENT,
    FIXING,
    PATCH_MERGE,
    PLAN_REVIEW,
    PLAN_JUDGEMENT,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEW_JUDGEMENT,
    REVIEWING,
    RUNNING,
    TESTING,
    TEST_JUDGEMENT,
)
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE, NEW_PROJECT
from harness.patch.gate import materialized_repo_markdown, run_patch_gate
from harness.testing.tester_result import TesterResult as HarnessTesterResult


from orchestrator_mock_support import _config


def _tester_result(tmp_path: Path, status: str) -> HarnessTesterResult:
    next_action = {
        "tests_passed": "continue",
        "source_bug": "fix_code",
        "environment_blocked": "block_task",
    }[status]
    env_issue = status == "environment_blocked"
    return HarnessTesterResult(
        status,
        next_action,
        status,
        status,
        tmp_path / "tester_result.json",
        {"status": status, "environment_dependency_issue": env_issue},
        env_issue,
    )


def test_artifact_visibility_rule_table_covers_role_phases() -> None:
    covered = {(rule.target_role, rule.target_phase) for rule in orchestrator_module.ARTIFACT_VISIBILITY_RULES}
    intentionally_empty_inputs = {
        ("executor", "MISC_RESPONSE"),
    }
    role_phases = {
        ("planner", PLANNING_DRAFT),
        ("planner", PLANNING_PEER_REVIEW),
        ("planner", PLANNING_REVISION),
        ("executor", EXECUTION),
        ("executor", PATCH_MERGE),
        ("executor", "MISC_RESPONSE"),
        ("executor", FIXING),
        ("executor", REVIEW_FIXING),
        ("tester", TESTING),
        ("tester", REGRESSION_TESTING),
        ("reviewer", PLAN_REVIEW),
        ("reviewer", REVIEWING),
        ("judge", TEST_JUDGEMENT),
        ("judge", REVIEW_JUDGEMENT),
        ("communicator", DELIVERY),
    }

    assert role_phases <= covered | intentionally_empty_inputs

def test_orchestrator_misc_flow_uses_executor_response_only(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("what does this dashboard mean?")

    response = orchestrator.run_task(task_id, workflow_type="misc")

    phases = [phase["phase_type"] for phase in orchestrator.repository.list_phases(task_id)]
    executor_runs = [run for run in orchestrator.repository.list_agent_runs(task_id) if run["role"] == "executor"]
    assert phases == ["MISC_RESPONSE"]
    assert len(executor_runs) == 1
    assert response.exists()
    assert response.name == "response.md"
    assert orchestrator.repository.list_artifacts(task_id, "response.md")


def test_orchestrator_records_task_classification_in_configuration(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("build a project", workflow_type=NEW_PROJECT)

    orchestrator.record_task_classification(
        task_id,
        {
            "workflow_type": NEW_PROJECT,
            "confidence": 0.9,
            "difficulty_score": 6,
            "difficulty_reason": "multi-service project",
            "reason": "new build request",
        },
    )

    task = orchestrator.repository.get_task(task_id)
    configuration = json.loads(task["configuration"])
    assert configuration["classification"]["workflow_type"] == NEW_PROJECT
    assert configuration["classification"]["difficulty_score"] == 6


def test_orchestrator_emits_progress_events(tmp_path: Path) -> None:
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(_config(tmp_path), progress_callback=events.append)
    task_id = orchestrator.create_task("implement a simple task")

    orchestrator.run_task(task_id)

    event_types = [event.event_type for event in events]
    assert "task_created" in event_types
    assert "phase_started" in event_types
    assert "agent_completed" in event_types
    assert event_types[-1] == "task_completed"
    completed_agent = next(event for event in events if event.event_type == "agent_completed")
    completed_phase = next(event for event in events if event.event_type == "phase_completed")
    assert "elapsed_seconds" in completed_agent.data
    assert "elapsed_seconds" in completed_phase.data
    assert completed_agent.data["elapsed_seconds"] >= 0
    assert completed_phase.data["elapsed_seconds"] >= 0
    assert completed_agent.trace_id == task_id
    assert completed_agent.span_id
    assert completed_agent.parent_span_id

def test_uncompleted_json_template_is_output_invalid_not_file_error(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["executor"]["count"] = 1
    config["limits"]["max_agent_retry"] = 0
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("merge without metadata")

    class MissingMetadataAdapter:
        def run(self, context):
            context.output_dir.mkdir(parents=True, exist_ok=True)
            context.log_dir.mkdir(parents=True, exist_ok=True)
            (context.output_dir / "merged_patch.diff").write_text("diff --git a/a b/a\n", encoding="utf-8")
            (context.output_dir / "delivery.md").write_text(
                json.dumps(
                    {
                        "return_code": 0,
                        "task_status": "success",
                        "role_return_code": 0,
                        "produced_files": context.required_outputs,
                        "known_risks": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            stdout = context.log_dir / "stdout.log"
            stderr = context.log_dir / "stderr.log"
            stdout.write_text("ok", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            return AgentRunResult(
                task_id=context.task_id,
                phase_id=context.phase_id,
                role=context.role,
                agent_id=context.agent_id,
                status="COMPLETED",
                exit_code=0,
                stdout_path=stdout,
                stderr_path=stderr,
            )

    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: MissingMetadataAdapter())

    try:
        orchestrator.run_role_phase(
            "executor",
            PATCH_MERGE,
            0,
            required_outputs_for("executor", PATCH_MERGE),
            "merge without metadata",
        )
    except Exception as exc:
        assert "merged_patch_metadata.json still contains Harness output template marker" in str(exc)
        assert "No such file or directory" not in str(exc)
    else:
        raise AssertionError("Expected uncompleted merged_patch_metadata.json template to fail validation")

    runs = orchestrator.repository.list_agent_runs(task_id)
    assert runs[-1]["status"] == "OUTPUT_INVALID"
    assert runs[-1]["error_message"] == "merged_patch_metadata.json still contains Harness output template marker"

def test_context_window_failure_is_not_retried(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["executor"]["count"] = 1
    config["limits"]["max_agent_retry"] = 2
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    task_id = orchestrator.create_task("large context")

    class ContextWindowAdapter:
        def run(self, context):
            context.log_dir.mkdir(parents=True, exist_ok=True)
            stdout = context.log_dir / "stdout.log"
            stderr = context.log_dir / "stderr.log"
            stdout.write_text("", encoding="utf-8")
            stderr.write_text(
                "ContextWindowExceededError: This model's maximum context length is 200000 tokens.",
                encoding="utf-8",
            )
            return AgentRunResult(
                task_id=context.task_id,
                phase_id=context.phase_id,
                role=context.role,
                agent_id=context.agent_id,
                status="FAILED",
                exit_code=1,
                stdout_path=stdout,
                stderr_path=stderr,
            )

    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: ContextWindowAdapter())

    try:
        orchestrator.run_role_phase(
            "executor",
            PATCH_MERGE,
            0,
            required_outputs_for("executor", PATCH_MERGE),
            "large context",
        )
    except Exception as exc:
        assert "exceeded the model context/request-size budget" in str(exc)
    else:
        raise AssertionError("Expected context-window failure")

    runs = orchestrator.repository.list_agent_runs(task_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "FAILED"
    assert any(event.event_type == "agent_failed" for event in events)
    assert not any(event.event_type == "agent_retryable_failure" for event in events)


def test_context_window_failure_with_exact_tokens_retries_with_downgraded_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config["roles"]["executor"]["count"] = 1
    config["limits"]["max_agent_retry"] = 1
    config["claude"] = {"context_window_tokens": 199999, "max_output_tokens": {"executor": 64000}}
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    task_id = orchestrator.create_task("large context")

    class ContextWindowThenSuccessAdapter:
        def __init__(self):
            self.seen_max_output_tokens: list[int | None] = []

        def run(self, context):
            context.log_dir.mkdir(parents=True, exist_ok=True)
            stdout = context.log_dir / "stdout.log"
            stderr = context.log_dir / "stderr.log"
            configured = context.config.get("claude", {}).get("max_output_tokens")
            self.seen_max_output_tokens.append(
                configured.get("executor") if isinstance(configured, dict) else configured
            )
            if len(self.seen_max_output_tokens) == 1:
                stdout.write_text(
                    "ContextWindowExceededError: This model's maximum context length is 200000 tokens. "
                    "However, you requested 64000 output tokens and your prompt contains at least "
                    "136001 input tokens, for a total of at least 200001 tokens. "
                    "(parameter=input_tokens, value=136001)",
                    encoding="utf-8",
                )
                stderr.write_text("", encoding="utf-8")
                return AgentRunResult(
                    task_id=context.task_id,
                    phase_id=context.phase_id,
                    role=context.role,
                    agent_id=context.agent_id,
                    status="FAILED",
                    exit_code=1,
                    stdout_path=stdout,
                    stderr_path=stderr,
                )
            context.output_dir.mkdir(parents=True, exist_ok=True)
            (context.output_dir / "response.md").write_text("artifact_result_code: 0\n\nok\n", encoding="utf-8")
            (context.output_dir / "notes.md").write_text("artifact_result_code: 0\n\nok\n", encoding="utf-8")
            (context.output_dir / "delivery.md").write_text(
                json.dumps(
                    {
                        "return_code": 0,
                        "task_status": "success",
                        "role_return_code": 0,
                        "produced_files": context.required_outputs,
                        "known_risks": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            stdout.write_text("ok", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            return AgentRunResult(
                task_id=context.task_id,
                phase_id=context.phase_id,
                role=context.role,
                agent_id=context.agent_id,
                status="COMPLETED",
                exit_code=0,
                stdout_path=stdout,
                stderr_path=stderr,
            )

    adapter = ContextWindowThenSuccessAdapter()
    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: adapter)

    results = orchestrator.run_role_phase(
        "executor",
        "MISC_RESPONSE",
        0,
        required_outputs_for("executor", "MISC_RESPONSE"),
        "large context",
    )

    assert len(results) == 1
    assert adapter.seen_max_output_tokens == [64000, 63998]
    runs = orchestrator.repository.list_agent_runs(task_id)
    assert [run["status"] for run in runs] == ["FAILED", "COMPLETED"]
    retry_event = next(event for event in events if event.event_type == "agent_retryable_failure")
    assert retry_event.data["next_max_output_tokens"] == 63998

def test_final_execution_fix_round_is_tested_before_exhaustion(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["limits"]["max_test_fix_rounds"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("repair invalid patch")
    validation_rounds: list[int] = []
    tested_rounds: list[int] = []

    def fake_patch_validation(task_id: str, round_id: int) -> bool:
        validation_rounds.append(round_id)
        return round_id == 1

    def fake_testing(task_id: str, phase: str, round_id: int, user_prompt: str, **kwargs) -> HarnessTesterResult:
        tested_rounds.append(round_id)
        return _tester_result(tmp_path, "tests_passed")

    monkeypatch.setattr(orchestrator, "_run_patch_validation", fake_patch_validation)
    monkeypatch.setattr(orchestrator.patch_gate_service, "try_skip_noop_candidate_patch", lambda *args, **kwargs: False)
    monkeypatch.setattr(orchestrator.workflow_engine, "run_testing_until_tester_decision", fake_testing)

    orchestrator._run_execution_test_loop(task_id, "repair invalid patch")

    phases = [(phase["phase_type"], phase["round_id"]) for phase in orchestrator.repository.list_phases(task_id)]
    assert validation_rounds == [0, 1]
    assert tested_rounds == [1]
    assert ("FIXING", 1) in phases

def test_agent_heartbeat_events_are_emitted_for_long_running_agents(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    config["mock"] = {"delay_seconds": 0.05}
    config["heartbeat"] = {"interval_seconds": 0.01}
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    orchestrator.create_task("plan with heartbeat")

    orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, required_outputs_for("planner", PLANNING_DRAFT), "plan with heartbeat")

    heartbeats = [event for event in events if event.event_type == "agent_heartbeat"]
    assert heartbeats
    assert heartbeats[0].role == "planner"
    assert heartbeats[0].status == "RUNNING"
