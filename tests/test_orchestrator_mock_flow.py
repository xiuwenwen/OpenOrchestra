from __future__ import annotations

import uuid
import re
from pathlib import Path
from concurrent.futures import wait as real_wait

from harness.agents.result import AgentRunResult, ArtifactRef
from harness.artifacts.schemas import required_outputs_for
import harness.core.orchestrator as orchestrator_module
from harness.core.orchestrator import Orchestrator
from harness.core.progress import ProgressEvent
from harness.core.state_machine import PATCH_MERGE, PLAN_JUDGEMENT, PLANNING_DRAFT, RUNNING
from harness.core.workflow_type import FEATURE_CHANGE, NEW_PROJECT


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
        "policy": {
            "different_roles_can_run_concurrently": False,
            "same_role_can_run_concurrently": True,
            "require_judge_final_approval": True,
            "allow_medium_bug_delivery": False,
            "require_all_tests_pass": True,
        },
    }


def test_orchestrator_mock_flow_completes_and_generates_delivery(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    task_id = orchestrator.create_task("implement a simple task")

    final_delivery = orchestrator.run_task(task_id)

    task = orchestrator.repository.get_task(task_id)
    task_started = next(event for event in events if event.event_type == "task_started")
    assert task_started.status == RUNNING
    assert task["status"] == "COMPLETED"
    assert final_delivery.exists()
    assert final_delivery.name == "final_delivery.md"
    assert "completed" in final_delivery.read_text(encoding="utf-8")
    assert orchestrator.repository.list_artifacts(task_id, "final_delivery.md")
    usage_guides = orchestrator.repository.list_artifacts(task_id, "usage_guide.md")
    assert usage_guides
    usage_guide = Path(usage_guides[-1]["path"])
    assert usage_guide.exists()
    assert "How To Use The Delivery" in usage_guide.read_text(encoding="utf-8")
    planner_run = next(run for run in orchestrator.repository.list_agent_runs(task_id) if run["role"] == "planner")
    planner_phase = next(phase for phase in orchestrator.repository.list_phases(task_id) if phase["phase_id"] == planner_run["phase_id"])
    prompt_path = (
        Path(config["system"]["workspace_root"])
        / task_id
        / planner_run["phase_id"]
        / "planner"
        / planner_run["agent_id"]
        / f"round_{planner_phase['round_id']}"
        / f"attempt_{planner_run['retry_count']}"
        / "logs"
        / "prompt.md"
    )
    assert prompt_path.exists()
    assert "Role: planner" in prompt_path.read_text(encoding="utf-8")


def test_orchestrator_bugfix_flow_skips_planning_and_completes(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("fix a failing command")

    final_delivery = orchestrator.run_task(task_id, workflow_type="bugfix")

    phases = [phase["phase_type"] for phase in orchestrator.repository.list_phases(task_id)]
    assert "PLANNING_DRAFT" not in phases
    assert "FIXING" in phases
    assert "TESTING" in phases
    assert final_delivery.exists()


def test_orchestrator_feature_change_flow_completes(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("add a feature")

    final_delivery = orchestrator.run_task(task_id, workflow_type="feature_change")

    phases = [phase["phase_type"] for phase in orchestrator.repository.list_phases(task_id)]
    assert phases[0] == "PLANNING_DRAFT"
    assert "EXECUTION" in phases
    assert "REVIEWING" in phases
    assert final_delivery.exists()


def test_planning_block_retries_until_judge_approves(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 1
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("add a feature")
    decisions: list[int] = []

    def fake_judge(task_id: str, phase: str, round_id: int, user_prompt: str) -> dict:
        decisions.append(round_id)
        if round_id == 0:
            return {"decision": "changes_required", "changes_required": True}
        return {"decision": "approved", "changes_required": False}

    monkeypatch.setattr(orchestrator, "_run_judge_phase", fake_judge)

    orchestrator._run_planning_block(task_id, "add a feature")

    planning_phases = [
        phase
        for phase in orchestrator.repository.list_phases(task_id)
        if phase["phase_type"] in {"PLANNING_DRAFT", "PLANNING_REVISION"}
    ]
    assert decisions == [0, 1]
    assert len(planning_phases) == 2
    assert [phase["phase_type"] for phase in planning_phases] == ["PLANNING_DRAFT", "PLANNING_REVISION"]


def test_planning_block_runs_peer_review_loop_and_plan_review(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["planner"]["count"] = 2
    config["limits"]["max_planning_rounds"] = 1
    config["limits"]["planning_peer_review_loops"] = 2
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("build peer reviewed plan")

    monkeypatch.setattr(
        orchestrator,
        "_run_judge_phase",
        lambda task_id, phase, round_id, user_prompt: {"decision": "approved", "changes_required": False},
    )

    orchestrator._run_planning_block(task_id, "build peer reviewed plan")

    phases = [(phase["phase_type"], phase["role"], phase["round_id"]) for phase in orchestrator.repository.list_phases(task_id)]
    assert ("PLANNING_DRAFT", "planner", 0) in phases
    assert ("PLANNING_PEER_REVIEW", "planner", 0) in phases
    assert ("PLANNING_REVISION", "planner", 1) in phases
    assert ("PLANNING_PEER_REVIEW", "planner", 1) in phases
    assert ("PLAN_REVIEW", "reviewer", 1) in phases
    assert orchestrator.repository.list_artifacts(task_id, "peer_review.md")
    assert orchestrator.repository.list_artifacts(task_id, "review_report.md")


def test_planner_retry_can_see_previous_planning_artifacts(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("revise plan")
    previous_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    current_phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 1)
    current_plan = tmp_path / "current-plan.md"
    current_plan.write_text("current", encoding="utf-8")
    for artifact_name in ("plan.md", "risk.md", "todo_breakdown.md"):
        path = tmp_path / artifact_name
        path.write_text(artifact_name, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=previous_phase_id,
                role="planner",
                agent_id="planner-1",
                artifact_type=artifact_name,
                path=path,
                version=1,
                hash="hash",
            )
        )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=current_phase_id,
            role="planner",
            agent_id="planner-1",
            artifact_type="plan.md",
            path=current_plan,
            version=2,
            hash="hash",
        )
    )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "input",
        "planner",
        "PLANNING_DRAFT",
        exclude_phase_id=current_phase_id,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "risk.md" in manifest
    assert "todo_breakdown.md" in manifest
    assert "current-plan.md" not in manifest


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


def test_same_role_agents_start_concurrently_when_configured(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["mock"] = {"delay_seconds": 0.05}
    events: list[ProgressEvent] = []
    orchestrator = Orchestrator(config, progress_callback=events.append)
    orchestrator.create_task("plan concurrent work")

    orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, required_outputs_for("planner", PLANNING_DRAFT), "plan concurrent work")

    relevant = [
        event
        for event in events
        if event.role == "planner" and event.event_type in {"agent_started", "agent_completed"}
    ]
    assert [event.event_type for event in relevant[:2]] == ["agent_started", "agent_started"]
    assert {event.agent_id for event in relevant if event.event_type == "agent_started"} == {"planner-1", "planner-2"}


def test_concurrent_phase_timeout_budget_includes_agent_retries(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["timeouts"]["planner"] = 7
    config["limits"]["max_agent_retry"] = 2
    captured_timeouts: list[float | None] = []
    orchestrator = Orchestrator(config)
    orchestrator.create_task("plan retry budget")

    def tracking_wait(futures, timeout=None):
        captured_timeouts.append(timeout)
        return real_wait(futures, timeout=timeout)

    monkeypatch.setattr(orchestrator_module, "wait", tracking_wait)

    orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, required_outputs_for("planner", PLANNING_DRAFT), "plan retry budget")

    assert captured_timeouts == [51]


def test_concurrent_phase_has_no_wait_timeout_when_role_timeout_is_zero(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["timeouts"]["planner"] = 0
    captured_timeouts: list[float | None] = []
    orchestrator = Orchestrator(config)
    orchestrator.create_task("plan without timeout")

    def tracking_wait(futures, timeout=None):
        captured_timeouts.append(timeout)
        return real_wait(futures, timeout=timeout)

    monkeypatch.setattr(orchestrator_module, "wait", tracking_wait)

    orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, required_outputs_for("planner", PLANNING_DRAFT), "plan without timeout")

    assert captured_timeouts == [None]


def test_failed_phase_with_completed_agent_runs_is_recovered_on_resume(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("recover old concurrent planner phase")
    phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    for agent_id in ("planner-1", "planner-2"):
        run_id = orchestrator.repository.create_agent_run(task_id, phase_id, "planner", agent_id, 0)
        for artifact_type in required_outputs_for("planner", "PLANNING_DRAFT"):
            path = tmp_path / f"{agent_id}-{artifact_type}"
            content = "status: success\n" if artifact_type == "delivery.md" else artifact_type
            path.write_text(content, encoding="utf-8")
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
    delivery.write_text("status: success\n", encoding="utf-8")
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
    old_delivery = tmp_path / "old-delivery.md"
    old_delivery.write_text("status: success\n", encoding="utf-8")
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
    for artifact_type in required_outputs_for("planner", PLANNING_DRAFT):
        path = tmp_path / f"latest-{artifact_type}"
        path.write_text("status: success\n" if artifact_type == "delivery.md" else artifact_type, encoding="utf-8")
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


def test_judge_checkpoint_resume_parses_existing_decision(tmp_path: Path) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("resume judge")
    orchestrator._active_task_id = task_id
    try:
        phase_id = orchestrator.repository.create_phase(task_id, PLAN_JUDGEMENT, "judge", 0)
        run_id = orchestrator.repository.create_agent_run(task_id, phase_id, "judge", "judge-1", 0)
        artifacts = {
            "decision.json": '{"decision":"approved","changes_required":false}\n',
            "decision_summary.md": "# Decision\napproved\n",
            "delivery.md": "status: success\n",
        }
        for artifact_type, content in artifacts.items():
            path = tmp_path / artifact_type
            path.write_text(content, encoding="utf-8")
            orchestrator.repository.create_artifact(
                ArtifactRef(
                    artifact_id=str(uuid.uuid4()),
                    task_id=task_id,
                    phase_id=phase_id,
                    role="judge",
                    agent_id="judge-1",
                    artifact_type=artifact_type,
                    path=path,
                    version=1,
                    hash="hash",
                )
            )
        orchestrator.repository.update_agent_run_status(run_id, "COMPLETED")
        orchestrator.repository.update_phase_status(phase_id, "COMPLETED")

        decision = orchestrator._run_judge_phase(task_id, PLAN_JUDGEMENT, 0, "resume judge")
    finally:
        orchestrator._active_task_id = None

    assert decision["decision"] == "approved"


def test_source_repo_is_used_only_for_existing_project_workflows(tmp_path: Path) -> None:
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    config = _config(tmp_path)
    config["system"]["source_repo"] = str(source_repo)
    orchestrator = Orchestrator(config)

    orchestrator._active_workflow_type = FEATURE_CHANGE
    assert orchestrator._source_repo_for_workspace() == source_repo.resolve()

    orchestrator._active_workflow_type = NEW_PROJECT
    assert orchestrator._source_repo_for_workspace() is None


def test_materialized_source_applies_modified_file_patch(tmp_path: Path) -> None:
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    (source_repo / "app.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    orchestrator = Orchestrator(_config(tmp_path))
    patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
"""

    files = orchestrator._materialized_files_from_unified_diff(patch, source_repo, include_modified=True)

    assert files[Path("app.py")] == ["one", "TWO", "three"]


def test_missing_delivery_md_is_output_invalid_not_file_error(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["executor"]["count"] = 1
    config["limits"]["max_agent_retry"] = 0
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("merge without delivery")

    class MissingDeliveryAdapter:
        def run(self, context):
            context.output_dir.mkdir(parents=True, exist_ok=True)
            context.log_dir.mkdir(parents=True, exist_ok=True)
            (context.output_dir / "merged_patch.diff").write_text("diff --git a/a b/a\n", encoding="utf-8")
            (context.output_dir / "merge_report.md").write_text("merged", encoding="utf-8")
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

    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: MissingDeliveryAdapter())

    try:
        orchestrator.run_role_phase(
            "executor",
            PATCH_MERGE,
            0,
            required_outputs_for("executor", PATCH_MERGE),
            "merge without delivery",
        )
    except Exception as exc:
        assert "Missing required output: delivery.md" in str(exc)
        assert "No such file or directory" not in str(exc)
    else:
        raise AssertionError("Expected missing delivery.md to fail validation")

    runs = orchestrator.repository.list_agent_runs(task_id)
    assert runs[-1]["status"] == "OUTPUT_INVALID"
    assert runs[-1]["error_message"] == "Missing required output: delivery.md"


def test_current_phase_artifacts_are_excluded_from_agent_inputs(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review work")
    previous_phase_id = orchestrator.repository.create_phase(task_id, "TESTING", "tester", 0)
    current_phase_id = orchestrator.repository.create_phase(task_id, "REVIEWING", "reviewer", 0)
    previous_artifact = tmp_path / "test_report.md"
    current_artifact = tmp_path / "review_report.md"
    previous_artifact.write_text("previous", encoding="utf-8")
    current_artifact.write_text("current", encoding="utf-8")
    for phase_id, path, agent_id in (
        (previous_phase_id, previous_artifact, "tester-1"),
        (current_phase_id, current_artifact, "reviewer-1"),
    ):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role=agent_id.split("-", 1)[0],
                agent_id=agent_id,
                artifact_type=path.name,
                path=path,
                version=1,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "reviewer", "REVIEWING", exclude_phase_id=current_phase_id)
    manifest = staged[0].read_text(encoding="utf-8")

    assert "test_report.md" in manifest
    assert "review_report.md" not in manifest


def test_staging_does_not_mutate_target_role_while_filtering(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("implement planned work")
    phase_id = orchestrator.repository.create_phase(task_id, "PLANNING_DRAFT", "planner", 0)
    artifact_names = ["plan.md", "assumptions.md", "risk.md", "todo_breakdown.md"]
    for artifact_name in artifact_names:
        path = tmp_path / artifact_name
        path.write_text(artifact_name, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role="planner",
                agent_id="planner-1",
                artifact_type=artifact_name,
                path=path,
                version=1,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "executor", "EXECUTION")
    manifest = staged[0].read_text(encoding="utf-8")

    for artifact_name in artifact_names:
        assert artifact_name in manifest


def test_judge_receives_merged_patch_for_test_judgement(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("judge merged patch")
    merge_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "orchestrator", 0)
    merged_patch = tmp_path / "merged_patch.diff"
    merged_patch.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=merge_phase_id,
            role="orchestrator",
            agent_id="patch-merger",
            artifact_type="merged_patch.diff",
            path=merged_patch,
            version=1,
            hash="hash",
        )
    )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "judge", "TEST_JUDGEMENT")
    manifest = staged[0].read_text(encoding="utf-8")

    assert "merged_patch.diff" in manifest
    assert "patch-merger" in manifest


def test_tester_sees_authoritative_merged_patch_not_raw_candidate_patches(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("test merged patch only")
    execution_phase_id = orchestrator.repository.create_phase(task_id, "EXECUTION", "executor", 0)
    merge_phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0)
    for artifact_type, phase_id, agent_id in [
        ("patch.diff", execution_phase_id, "executor-1"),
        ("fix_patch.diff", execution_phase_id, "executor-2"),
        ("merged_patch.diff", merge_phase_id, "executor-1"),
        ("merge_report.md", merge_phase_id, "executor-1"),
    ]:
        path = tmp_path / artifact_type
        path.write_text(artifact_type, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role="executor",
                agent_id=agent_id,
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "tester", "TESTING")
    manifest = staged[0].read_text(encoding="utf-8")

    assert "merged_patch.diff" in manifest
    assert "merge_report.md" in manifest
    assert not re.search(r"## \d+\. patch\.diff\b", manifest)
    assert not re.search(r"## \d+\. fix_patch\.diff\b", manifest)


def test_staged_input_artifacts_respect_size_budget(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["artifact_input"] = {"max_files": 1, "max_file_bytes": 40, "max_total_bytes": 60}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("review large artifacts")
    phase_id = orchestrator.repository.create_phase(task_id, "TESTING", "tester", 0)
    large_report = tmp_path / "test_report.md"
    large_report.write_text("A" * 200, encoding="utf-8")
    bug_report = tmp_path / "bug_report.md"
    bug_report.write_text("bug", encoding="utf-8")
    for artifact_type, path in (("bug_report.md", bug_report), ("test_report.md", large_report)):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role="tester",
                agent_id="tester-1",
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "reviewer", "REVIEWING")
    manifest = staged[0].read_text(encoding="utf-8")

    assert len(staged) == 2
    assert "truncated: true" in manifest
    assert "skipped: true" in manifest
    assert staged[1].stat().st_size < large_report.stat().st_size


def test_delivery_is_published_to_shallow_deliver_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["system"]["deliver_root"] = str(tmp_path / "deliver")
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("Build Weather Tool")

    final_delivery = orchestrator.run_task(task_id)

    merge_phases = [
        phase for phase in orchestrator.repository.list_phases(task_id) if phase["phase_type"] == "PATCH_MERGE"
    ]
    assert merge_phases
    assert {phase["role"] for phase in merge_phases} == {"executor"}
    assert final_delivery == tmp_path / "deliver" / f"build-weather-tool-{task_id[:8]}" / "final_delivery.md"
    assert final_delivery.exists()
    assert (final_delivery.parent / "success_path.md").exists()
    assert (final_delivery.parent / "usage_guide.md").exists()
    assert (final_delivery.parent / "patches" / "final.patch").exists()
    assert (final_delivery.parent / "artifacts" / "merged_patch.diff").exists()
    assert (final_delivery.parent / "artifacts" / "merge_report.md").exists()
    assert (final_delivery.parent / "artifacts" / "patch.diff").exists()
    assert (final_delivery.parent / "source" / "mock.txt").read_text(encoding="utf-8") == "mock change\n"
    merged_artifacts = orchestrator.repository.list_artifacts(task_id, "merged_patch.diff")
    success_path_artifacts = orchestrator.repository.list_artifacts(task_id, "success_path.md")
    assert merged_artifacts
    assert success_path_artifacts
    assert Path(success_path_artifacts[-1]["path"]) == final_delivery.parent / "success_path.md"
    assert (final_delivery.parent / "patches" / "final.patch").read_text(encoding="utf-8") == Path(
        merged_artifacts[-1]["path"]
    ).read_text(encoding="utf-8")
    manifest = (final_delivery.parent / "artifacts_manifest.md").read_text(encoding="utf-8")
    success_path = (final_delivery.parent / "success_path.md").read_text(encoding="utf-8")
    assert f"success_path: {final_delivery.parent}" in manifest
    assert f"success_path: {final_delivery.parent}" in success_path
    assert "patches/final.patch" in manifest
    assert "source/mock.txt" in manifest


def test_delivery_project_name_uses_ascii_safe_slug(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["system"]["deliver_root"] = str(tmp_path / "deliver")
    orchestrator = Orchestrator(config)
    task_id = "859fe499-d655-455d-933d-34021a4aea67"

    assert orchestrator._slugify_project_name("做个双人对战的象棋游戏") == "project"
    assert orchestrator._slugify_project_name("做个 Chinese Chess Game!") == "chinese-chess-game"
    assert orchestrator._delivery_project_dir(task_id, "做个双人对战的象棋游戏").name == "project-859fe499"


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
