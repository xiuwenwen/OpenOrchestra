from __future__ import annotations

import json
import sys
import uuid
import re
from pathlib import Path
from concurrent.futures import wait as real_wait

import pytest

from harness.adapters.command_runner import CapturedCommandResult
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


def _write_tester_result(path: Path, status: str) -> None:
    next_action = {
        "tests_passed": "continue",
        "source_bug": "fix_code",
        "environment_blocked": "block_task",
    }[status]
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "next_action": next_action,
                "failure_type": "none" if status == "tests_passed" else status,
                "environment_dependency_issue": status == "environment_blocked",
                "summary": status,
                "setup_commands_run": [],
                "test_commands_run": [],
                "oracle_results": [
                    {
                        "oracle_id": "A1",
                        "status": "blocked" if status == "environment_blocked" else ("passed" if status == "tests_passed" else "failed"),
                        "evidence": status,
                        "commands_run": ["pytest"],
                        "output_excerpt": "",
                    }
                ],
                "remaining_blockers": [],
            }
        ),
        encoding="utf-8",
    )


def test_regression_testing_uses_tester_result_without_test_gate(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review fix", workflow_type=BUGFIX)
    tested_rounds: list[int] = []

    monkeypatch.setattr(orchestrator, "run_role_phase", lambda *args, **kwargs: [])

    def fake_testing(task_id: str, phase: str, round_id: int, user_prompt: str, **kwargs) -> HarnessTesterResult:
        tested_rounds.append(round_id)
        return _tester_result(tmp_path, "tests_passed")

    monkeypatch.setattr(orchestrator.workflow_engine, "run_testing_until_tester_decision", fake_testing)

    orchestrator._run_regression_test_fix_loop(task_id, "review fix", review_round_id=1, merge_ok=True)

    assert tested_rounds == [1]

def test_patch_validation_uses_project_context_source_repo(tmp_path: Path) -> None:
    configured_repo = tmp_path / "configured"
    configured_repo.mkdir()
    (configured_repo / "app.py").write_text("wrong\n", encoding="utf-8")
    historical_source = tmp_path / "deliver" / "project-12345678" / "source"
    historical_source.mkdir(parents=True)
    (historical_source / "app.py").write_text("old\n", encoding="utf-8")
    config = _config(tmp_path)
    config["system"]["source_repo"] = str(configured_repo)
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("fix the previous delivery", workflow_type=BUGFIX)
    orchestrator.attach_project_context(task_id, f"Historical success_path: {historical_source.parent}\n")
    phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0, status="COMPLETED")
    patch = tmp_path / "merged_patch.diff"
    patch.write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id="merged-patch",
            task_id=task_id,
            phase_id=phase_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="merged_patch.diff",
            path=patch,
            version=1,
            hash="hash",
        )
    )

    assert orchestrator._run_patch_validation(task_id, 0)
    validation_report = Path(orchestrator.repository.list_artifacts(task_id, "patch_validation.md")[-1]["path"]).read_text(encoding="utf-8")
    materialized_report = Path(orchestrator.repository.list_artifacts(task_id, "materialized_repo.md")[-1]["path"]).read_text(encoding="utf-8")
    objective_report = Path(orchestrator.repository.list_artifacts(task_id, "objective_gate.md")[-1]["path"]).read_text(encoding="utf-8")
    gate_payload = json.loads(Path(orchestrator.repository.list_artifacts(task_id, "patch_gate_result.json")[-1]["path"]).read_text(encoding="utf-8"))
    materialized_app = orchestrator._latest_materialized_repo(task_id) / "app.py"

    assert "source_repo: orchestrator_private_source_repo" in validation_report
    assert "source_repo: orchestrator_private_source_repo" in materialized_report
    assert str(historical_source.resolve()) not in validation_report
    assert str(historical_source.resolve()) not in materialized_report
    assert "status: pass" in objective_report
    assert gate_payload["status"] == "pass"
    assert gate_payload["failure_type"] == "none"
    assert gate_payload["apply_base"] == "orchestrator_private_source_repo"
    assert "diff_check_status: pass" in materialized_report
    assert materialized_app.read_text(encoding="utf-8") == "new\n"

def test_objective_patch_gate_rejects_sensitive_files(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("do not write secrets", workflow_type=BUGFIX)
    phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0, status="COMPLETED")
    patch = tmp_path / "merged_patch.diff"
    patch.write_text(
        "diff --git a/.env b/.env\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/.env\n"
        "@@ -0,0 +1 @@\n"
        "+TOKEN=secret\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id="sensitive-patch",
            task_id=task_id,
            phase_id=phase_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="merged_patch.diff",
            path=patch,
            version=1,
            hash="hash",
        )
    )

    assert not orchestrator._run_patch_validation(task_id, 0)
    objective_report = Path(orchestrator.repository.list_artifacts(task_id, "objective_gate.md")[-1]["path"]).read_text(encoding="utf-8")
    gate_payload = json.loads(Path(orchestrator.repository.list_artifacts(task_id, "patch_gate_result.json")[-1]["path"]).read_text(encoding="utf-8"))

    assert "status: fail" in objective_report
    assert "scope_status: fail" in objective_report
    assert "forbidden sensitive file path: .env" in objective_report
    assert gate_payload["status"] == "fail"
    assert gate_payload["failure_type"] == "patch_scope"
    assert "forbidden sensitive file path: .env" in gate_payload["precheck_errors"]

def test_harness_test_gate_runs_detected_pytest(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("run tests", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_latest_materialized_repo", lambda task_id: repo)

    assert orchestrator._run_harness_test_gate(task_id, 0)
    report = Path(orchestrator.repository.list_artifacts(task_id, "test_gate.md")[-1]["path"]).read_text(encoding="utf-8")

    assert "status: pass" in report
    assert "exit_code: 0" in report


def test_harness_test_gate_runs_nested_package_tests(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("run package tests", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    package_tests = repo / "pkg" / "tests"
    package_tests.mkdir(parents=True)
    (package_tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_latest_materialized_repo", lambda task_id: repo)

    assert orchestrator._run_harness_test_gate(task_id, 0)
    report = Path(orchestrator.repository.list_artifacts(task_id, "test_gate.md")[-1]["path"]).read_text(encoding="utf-8")

    assert "-m pytest -q" in report
    assert "status: pass" in report

def test_harness_test_gate_records_timeout_failure(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["testing"] = {
        "runtime": "native",
        "timeout_seconds": 1,
    }
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("timeout test", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    orchestrator.test_gate_service.latest_materialized_repo = lambda task_id: repo

    assert not orchestrator.test_gate_service.run_gate(
        task_id,
        0,
        artifact_type="test_gate.md",
        title="Harness Test Gate",
        log_dir_name="test_gate_logs",
        commands=[f"{sys.executable} -c \"import time; time.sleep(2)\""],
        require_commands=True,
    )
    report = Path(orchestrator.repository.list_artifacts(task_id, "test_gate.md")[-1]["path"]).read_text(encoding="utf-8")

    assert "status: fail" in report
    assert "exit_code: timeout" in report

def test_harness_test_gate_does_not_execute_shell_metacharacters(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "shell_injection_marker"
    config = _config(tmp_path)
    config["testing"] = {
        "runtime": "native",
        "timeout_seconds": 5,
    }
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("reject shell metacharacters", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    orchestrator.test_gate_service.latest_materialized_repo = lambda task_id: repo

    assert orchestrator.test_gate_service.run_gate(
        task_id,
        0,
        artifact_type="test_gate.md",
        title="Harness Test Gate",
        log_dir_name="test_gate_logs",
        commands=[f"{sys.executable} -c \"print('ok')\"; touch {marker}"],
        require_commands=True,
    )

    assert not marker.exists()
    report = Path(orchestrator.repository.list_artifacts(task_id, "test_gate.md")[-1]["path"]).read_text(encoding="utf-8")
    assert "status: pass" in report

def test_harness_test_gate_uses_compileall_for_python_repo_without_tests(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("compile python", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_latest_materialized_repo", lambda task_id: repo)

    assert orchestrator._run_harness_test_gate(task_id, 0)
    report = Path(orchestrator.repository.list_artifacts(task_id, "test_gate.md")[-1]["path"]).read_text(encoding="utf-8")

    assert "compileall -q ." in report
    assert "status: pass" in report

def test_harness_test_gate_ignores_tester_result_for_command_selection(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("ignore tester command", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_latest_materialized_repo", lambda task_id: repo)
    test_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 0, status="COMPLETED")
    command = f"{sys.executable} -c \"print('from tester env')\""
    result_path = tmp_path / "tester_result.json"
    _write_tester_result(result_path, "tests_passed")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=test_phase_id,
            role="tester",
            agent_id="tester-1",
            artifact_type="tester_result.json",
            path=result_path,
            version=1,
            hash="hash",
        )
    )

    assert orchestrator._run_harness_test_gate(task_id, 0)
    report = Path(orchestrator.repository.list_artifacts(task_id, "test_gate.md")[-1]["path"]).read_text(encoding="utf-8")

    assert "compileall -q ." in report
    assert command not in report

def test_tester_setup_policy_rejects_unsafe_package(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()

    error = orchestrator.test_gate_service.tester_setup_command_policy_error(
        repo,
        f"{sys.executable} -m pip install pandas",
    )

    assert "not project-declared or minimal test tooling" in (error or "")


def test_harness_test_gate_reuses_results_for_same_repo_and_commands(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["testing"] = {"runtime": "native", "timeout_seconds": 5}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("cache test gate", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('same')\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_latest_materialized_repo", lambda task_id: repo)

    class CountingRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run_capture(self, *args, **kwargs) -> CapturedCommandResult:
            self.calls += 1
            return CapturedCommandResult(returncode=0, stdout="ok\n", stderr="")

    runner = CountingRunner()
    orchestrator.test_gate_service.command_runner = runner

    assert orchestrator._run_harness_test_gate(task_id, 0)
    assert orchestrator._run_harness_test_gate(task_id, 1)

    reports = orchestrator.repository.list_artifacts(task_id, "test_gate.md")
    latest = Path(reports[-1]["path"]).read_text(encoding="utf-8")
    assert runner.calls == 1
    assert '"cache_hit": true' in latest
    assert "status: pass" in latest

def test_patch_validation_selects_merged_patch_from_requested_round(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("fix with multiple rounds", workflow_type=BUGFIX)
    first_phase = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0, status="COMPLETED")
    second_phase = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 1, status="COMPLETED")
    first_patch = tmp_path / "round0.patch"
    second_patch = tmp_path / "round1.patch"
    first_patch.write_text("round0\n", encoding="utf-8")
    second_patch.write_text("round1\n", encoding="utf-8")
    for version, phase_id, path in ((1, first_phase, first_patch), (2, second_phase, second_patch)):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=f"merged-patch-{version}",
                task_id=task_id,
                phase_id=phase_id,
                role="executor",
                agent_id="executor-1",
                artifact_type="merged_patch.diff",
                path=path,
                version=version,
                hash="hash",
            )
        )

    selected = orchestrator._latest_merged_patch_for_round(task_id, 0)

    assert selected
    assert Path(selected["path"]) == first_patch

def test_materialization_diff_check_catches_new_file_whitespace(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("bad whitespace", workflow_type=BUGFIX)
    patch = tmp_path / "bad.patch"
    patch.write_text(
        "diff --git a/bad.py b/bad.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/bad.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1 \n",
        encoding="utf-8",
    )

    result = run_patch_gate(
        patch_path=patch,
        source_repo=None,
        materialized_repo_dir=orchestrator._materialized_repo_dir(task_id, 0),
    )
    report = materialized_repo_markdown(result, task_id, 0)

    assert result.materialized_repo is None
    assert "status: failed" in report
    assert "diff_check_status: fail" in report

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

def test_failed_materialization_does_not_leave_usable_repo(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("bad patch")
    patch = tmp_path / "bad.patch"
    patch.write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-missing\n"
        "+present\n",
        encoding="utf-8",
    )

    result = run_patch_gate(
        patch_path=patch,
        source_repo=None,
        materialized_repo_dir=orchestrator._materialized_repo_dir(task_id, 0),
    )
    report = materialized_repo_markdown(result, task_id, 0)

    assert result.materialized_repo is None
    assert "status: skipped" in report
    assert not orchestrator._materialized_repo_dir(task_id, 0).exists()
    assert orchestrator._latest_materialized_repo(task_id) is None

def test_latest_materialized_repo_falls_back_to_previous_success_artifact_and_marker(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("materialized freshness")
    success_repo = orchestrator._materialized_repo_dir(task_id, 0)
    success_repo.mkdir(parents=True)
    patch = tmp_path / "ok.patch"
    patch.write_text("diff --git a/a.txt b/a.txt\n", encoding="utf-8")
    orchestrator._write_materialized_success_marker(success_repo, task_id, 0, patch)
    success_report = tmp_path / "materialized-success.md"
    success_report.write_text(
        "\n".join(["# Materialized Repository", "", "status: success", f"task_id: {task_id}", "round_id: 0", f"repo_path: {success_repo}", ""]),
        encoding="utf-8",
    )
    failure_report = tmp_path / "materialized-failure.md"
    failure_report.write_text(
        "\n".join(["# Materialized Repository", "", "status: failed", f"task_id: {task_id}", "round_id: 1", "repo_path: none", ""]),
        encoding="utf-8",
    )
    phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0, status="COMPLETED")
    for index, path in enumerate((success_report, failure_report), start=1):
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=f"materialized-{index}",
                task_id=task_id,
                phase_id=phase_id,
                role="orchestrator",
                agent_id="patch-materializer",
                artifact_type="materialized_repo.md",
                path=path,
                version=index,
                hash="hash",
            )
        )

    assert orchestrator._latest_materialized_repo(task_id) == success_repo


def test_materialized_workspace_repo_excludes_generated_runtime_screenshots(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("copy materialized source")
    success_repo = orchestrator._materialized_repo_dir(task_id, 0)
    (success_repo / "output" / "screenshots").mkdir(parents=True)
    (success_repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (success_repo / "output" / ".gitkeep").write_text("", encoding="utf-8")
    (success_repo / "output" / "screenshots" / "runtime.png").write_bytes(b"png")
    patch = tmp_path / "ok.patch"
    patch.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
    orchestrator._write_materialized_success_marker(success_repo, task_id, 0, patch)
    success_report = tmp_path / "materialized-success.md"
    success_report.write_text(
        "\n".join(
            [
                "# Materialized Repository",
                "",
                "status: success",
                f"task_id: {task_id}",
                "round_id: 0",
                f"repo_path: {success_repo}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    phase_id = orchestrator.repository.create_phase(task_id, "PATCH_MERGE", "executor", 0, status="COMPLETED")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id="materialized-success",
            task_id=task_id,
            phase_id=phase_id,
            role="orchestrator",
            agent_id="patch-materializer",
            artifact_type="materialized_repo.md",
            path=success_report,
            version=1,
            hash="hash",
        )
    )
    workspace_repo = tmp_path / "workspace" / "repo"

    orchestrator._prepare_materialized_workspace_repo(task_id, "executor", FIXING, workspace_repo)

    assert (workspace_repo / "app.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert (workspace_repo / "output" / ".gitkeep").exists()
    assert not (workspace_repo / "output" / "screenshots").exists()
    assert (workspace_repo / ".git").is_dir()


def test_single_candidate_patch_merge_uses_deterministic_fast_path(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("deterministic patch merge")
    execution_phase_id = orchestrator.repository.create_phase(task_id, EXECUTION, "executor", 0, status="COMPLETED")
    patch = tmp_path / "patch.diff"
    patch.write_text(
        "diff --git a/app.py b/app.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/app.py\n"
        "@@ -0,0 +1 @@\n"
        "+print('ok')\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=execution_phase_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="patch.diff",
            path=patch,
            version=1,
            hash="hash",
        )
    )

    assert orchestrator.run_patch_merge(task_id, 0, "deterministic patch merge") is True

    phases = [phase for phase in orchestrator.repository.list_phases(task_id) if phase["phase_type"] == PATCH_MERGE]
    assert len(phases) == 1
    runs = [run for run in orchestrator.repository.list_agent_runs(task_id) if run["phase_id"] == phases[0]["phase_id"]]
    assert [run["agent_id"] for run in runs] == ["deterministic-patch-merge"]
    metadata = json.loads(
        Path(orchestrator.repository.list_artifacts(task_id, "merged_patch_metadata.json")[-1]["path"]).read_text(encoding="utf-8")
    )
    assert metadata["merge_report"]["merge_strategy"] == "deterministic_single_candidate"
    assert metadata["changed_files"] == ["app.py"]
    assert metadata["expected_apply_command"] == "git apply --whitespace=nowarn merged_patch.diff"
    assert "status: pass" in Path(orchestrator.repository.list_artifacts(task_id, "patch_validation.md")[-1]["path"]).read_text(encoding="utf-8")


def test_patch_gate_ignores_previous_prompt_turn_candidates(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("patch followup", workflow_type=BUGFIX)
    old_phase_id = orchestrator.repository.create_phase(task_id, EXECUTION, "executor", 0, status="COMPLETED")
    old_patch = tmp_path / "old.patch"; old_patch.write_text("old\n", encoding="utf-8")
    orchestrator.repository.create_artifact(ArtifactRef(str(uuid.uuid4()), task_id, old_phase_id, "executor", "executor-1", "patch.diff", old_patch, 1, "hash"))
    orchestrator.repository.append_task_prompt_turn(task_id, "second turn")
    current_phase_id = orchestrator.repository.create_phase(task_id, EXECUTION, "executor", 0, status="COMPLETED")
    current_patch = tmp_path / "current.patch"; current_patch.write_text("current\n", encoding="utf-8")
    orchestrator.repository.create_artifact(ArtifactRef(str(uuid.uuid4()), task_id, current_phase_id, "executor", "executor-1", "patch.diff", current_patch, 2, "hash"))

    candidates = orchestrator.patch_gate_service.current_round_candidate_patches(task_id, 0)

    assert [Path(candidate["path"]) for candidate in candidates] == [current_patch]


def test_empty_candidate_patch_skips_merge_model_and_testing(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("skip empty patch", workflow_type=BUGFIX)
    execution_phase_id = orchestrator.repository.create_phase(task_id, EXECUTION, "executor", 0, status="COMPLETED")
    patch = tmp_path / "patch.diff"
    patch.write_text("\n", encoding="utf-8")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=execution_phase_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="patch.diff",
            path=patch,
            version=1,
            hash="hash",
        )
    )

    assert orchestrator.run_patch_merge(task_id, 0, "skip empty patch") is False

    assert not orchestrator.repository.list_artifacts(task_id, "merged_patch.diff")
    objective = Path(orchestrator.repository.list_artifacts(task_id, "objective_gate.md")[-1]["path"]).read_text(encoding="utf-8")
    assert "status: fail" in objective
    assert "empty_patch" in objective


def test_empty_candidate_patch_is_accepted_after_passing_tester_result(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("accept no-op patch", workflow_type=BUGFIX)
    test_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 0, status="COMPLETED")
    tester_result = tmp_path / "tester_result.json"
    _write_tester_result(tester_result, "tests_passed")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=test_phase_id,
            role="tester",
            agent_id="tester-1",
            artifact_type="tester_result.json",
            path=tester_result,
            version=1,
            hash="hash",
        )
    )
    fixing_phase_id = orchestrator.repository.create_phase(task_id, REVIEW_FIXING, "executor", 1, status="COMPLETED")
    patch = tmp_path / "fix_patch.diff"
    patch.write_text("\n", encoding="utf-8")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=fixing_phase_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="fix_patch.diff",
            path=patch,
            version=1,
            hash="hash",
        )
    )

    assert orchestrator.run_patch_merge(task_id, 1, "accept no-op patch") is True

    objective = Path(orchestrator.repository.list_artifacts(task_id, "objective_gate.md")[-1]["path"]).read_text(encoding="utf-8")
    assert "status: pass" in objective
    assert "accepted_empty_patch" in objective
    assert not orchestrator.repository.list_artifacts(task_id, "merged_patch.diff")


def test_duplicate_candidate_patch_skips_retesting_previous_patch(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("skip duplicate patch", workflow_type=BUGFIX)
    round0_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 0, status="COMPLETED")
    previous = tmp_path / "previous.diff"
    patch_text = (
        "diff --git a/app.py b/app.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/app.py\n"
        "@@ -0,0 +1 @@\n"
        "+print('ok')\n"
    )
    previous.write_text(patch_text, encoding="utf-8")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=round0_phase_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="merged_patch.diff",
            path=previous,
            version=1,
            hash="hash",
        )
    )
    fixing_phase_id = orchestrator.repository.create_phase(task_id, FIXING, "executor", 1, status="COMPLETED")
    duplicate = tmp_path / "duplicate.diff"
    duplicate.write_text(patch_text, encoding="utf-8")
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=fixing_phase_id,
            role="executor",
            agent_id="executor-1",
            artifact_type="fix_patch.diff",
            path=duplicate,
            version=1,
            hash="hash",
        )
    )

    assert orchestrator.run_patch_merge(task_id, 1, "skip duplicate patch") is False

    objective = Path(orchestrator.repository.list_artifacts(task_id, "objective_gate.md")[-1]["path"]).read_text(encoding="utf-8")
    assert "duplicate_previous_merged_patch" in objective

def test_invalid_patch_merge_skips_testing_and_enters_fix_round(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("repair invalid patch")
    validation_rounds: list[int] = []

    def fake_patch_validation(task_id: str, round_id: int) -> bool:
        validation_rounds.append(round_id)
        if round_id <= 0:
            return False
        gate = tmp_path / f"objective_gate_round_{round_id}.md"
        gate.write_text(
            "# Objective Gate\n\n"
            "status: pass\n"
            f"task_id: {task_id}\n"
            f"round_id: {round_id}\n"
            "legal_unified_diff: true\n"
            "scope_status: pass\n"
            "size_status: pass\n"
            "patch_apply_status: pass\n"
            "materialize_status: success\n"
            "diff_check_status: pass\n"
            "changed_line_count: 1\n"
            "deleted_file_count: 0\n",
            encoding="utf-8",
        )
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=f"objective-gate-{round_id}",
                task_id=task_id,
                phase_id=None,
                role="orchestrator",
                agent_id="objective-gate",
                artifact_type="objective_gate.md",
                path=gate,
                version=round_id,
                hash="hash",
            )
        )
        return True

    monkeypatch.setattr(orchestrator, "_run_patch_validation", fake_patch_validation)
    monkeypatch.setattr(orchestrator.patch_gate_service, "try_skip_noop_candidate_patch", lambda *args, **kwargs: False)

    orchestrator._run_execution_test_loop(task_id, "repair invalid patch")

    phases = [(phase["phase_type"], phase["round_id"]) for phase in orchestrator.repository.list_phases(task_id)]
    assert validation_rounds[:2] == [0, 1]
    assert ("FIXING", 1) in phases
    assert ("TESTING", 0) not in phases
    assert ("TESTING", 1) in phases
