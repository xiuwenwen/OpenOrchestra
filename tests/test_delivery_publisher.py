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


from orchestrator_mock_support import _config


def test_delivery_contract_review_accepts_format_only_failure(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["roles"]["executor"]["count"] = 1
    config["limits"]["max_agent_retry"] = 0
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("merge with malformed delivery")

    class MalformedDeliveryAdapter:
        def run(self, context):
            context.output_dir.mkdir(parents=True, exist_ok=True)
            context.log_dir.mkdir(parents=True, exist_ok=True)
            (context.output_dir / "merged_patch.diff").write_text("diff --git a/a b/a\n", encoding="utf-8")
            (context.output_dir / "merged_patch_metadata.md").write_text(
                "artifact_result_code: 0\n\npatch_artifact: merged_patch.diff\n", encoding="utf-8"
            )
            (context.output_dir / "merge_report.md").write_text("artifact_result_code: 0\n\nmerged", encoding="utf-8")
            (context.output_dir / "delivery.md").write_text("status: success\nsummary: completed\n", encoding="utf-8")
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

    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: MalformedDeliveryAdapter())

    results = orchestrator.run_role_phase(
        "executor",
        PATCH_MERGE,
        0,
        required_outputs_for("executor", PATCH_MERGE),
        "merge with malformed delivery",
    )

    delivery_artifact = next(artifact for artifact in results[0].artifacts if artifact.artifact_type == "delivery.md")
    payload = json.loads(delivery_artifact.path.read_text(encoding="utf-8"))
    runs = orchestrator.repository.list_agent_runs(task_id)

    assert runs[-1]["status"] == "COMPLETED"
    assert payload["return_code"] == 0
    assert payload["contract_review"]["decision"] == "accept"
    assert (Path(payload["contract_review"]["prompt_path"]).parent / "delivery.original.md").exists()

def test_final_handoff_stages_lean_delivery_evidence(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("finalize latest evidence")
    old_plan_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_DRAFT, "planner", 0)
    latest_plan_phase_id = orchestrator.repository.create_phase(task_id, PLANNING_REVISION, "planner", 2)
    selected_plan_phase_id = orchestrator.repository.create_phase(task_id, PLAN_REVIEW, "reviewer", 1)
    old_exec_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 1)
    latest_exec_phase_id = orchestrator.repository.create_phase(task_id, PATCH_MERGE, "executor", 2)
    old_test_phase_id = orchestrator.repository.create_phase(task_id, TESTING, "tester", 1)
    latest_test_phase_id = orchestrator.repository.create_phase(task_id, REGRESSION_TESTING, "tester", 2)
    old_review_phase_id = orchestrator.repository.create_phase(task_id, REVIEWING, "reviewer", 0)
    latest_review_phase_id = orchestrator.repository.create_phase(task_id, REVIEWING, "reviewer", 1)
    old_judge_phase_id = orchestrator.repository.create_phase(task_id, TEST_JUDGEMENT, "judge", 1)
    latest_judge_phase_id = orchestrator.repository.create_phase(task_id, REVIEW_JUDGEMENT, "judge", 2)

    artifact_rows = [
        ("plan.md", old_plan_phase_id, "planner", "old-plan.md", "planner-1"),
        ("plan.md", latest_plan_phase_id, "planner", "latest-plan.md", "planner-1"),
        ("merged_patch.diff", old_exec_phase_id, "executor", "old-merged.patch", "executor-1"),
        ("merged_patch.diff", latest_exec_phase_id, "executor", "latest-merged.patch", "executor-1"),
        ("merged_patch_metadata.md", latest_exec_phase_id, "executor", "latest-merged-metadata.md", "executor-1"),
        ("changed_files.md", latest_exec_phase_id, "executor", "latest-changed-files.md", "executor-1"),
        ("self_check.md", latest_exec_phase_id, "executor", "latest-self-check.md", "executor-1"),
        ("fix_notes.md", latest_exec_phase_id, "executor", "latest-fix-notes.md", "executor-1"),
        ("merge_report.md", latest_exec_phase_id, "executor", "latest-merge-report.md", "executor-1"),
        ("patch.diff", latest_exec_phase_id, "executor", "latest-candidate.patch", "executor-1"),
        ("fix_patch.diff", latest_exec_phase_id, "executor", "latest-fix-candidate.patch", "executor-1"),
        ("bug_report.md", old_test_phase_id, "tester", "old-bug-report.md", "tester-1"),
        ("bug_report.md", latest_test_phase_id, "tester", "latest-bug-report.md", "tester-1"),
        ("selected_plan.md", selected_plan_phase_id, "reviewer", "latest-selected-plan.md", "reviewer-1"),
        ("review_report.md", old_review_phase_id, "reviewer", "old-review-report.md", "reviewer-1"),
        ("review_report.md", latest_review_phase_id, "reviewer", "latest-review-report.md", "reviewer-1"),
        ("decision.json", old_judge_phase_id, "judge", "old-decision.json", "judge-1"),
        ("decision.json", latest_judge_phase_id, "judge", "latest-decision.json", "judge-1"),
    ]
    for artifact_type, phase_id, role, filename, agent_id in artifact_rows:
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=phase_id,
                role=role,
                agent_id=agent_id,
                artifact_type=artifact_type,
                path=path,
                version=1,
                hash="hash",
            )
        )

    for round_id in (1, 2):
        path = tmp_path / f"final-round-{round_id}-test_gate.md"
        path.write_text(f"# test_gate.md\n\nround_id: {round_id}\n", encoding="utf-8")
        orchestrator.repository.create_artifact(
            ArtifactRef(
                artifact_id=str(uuid.uuid4()),
                task_id=task_id,
                phase_id=None,
                role="orchestrator",
                agent_id="orchestrator",
                artifact_type="test_gate.md",
                path=path,
                version=round_id,
                hash="hash",
            )
        )

    staged = orchestrator._stage_input_artifacts(
        task_id,
        tmp_path / "communicator-input",
        "communicator",
        DELIVERY,
        round_id=0,
    )
    manifest = staged[0].read_text(encoding="utf-8")

    assert "latest-merged-metadata.md" in manifest
    assert "latest-changed-files.md" in manifest
    assert "latest-self-check.md" in manifest
    assert "latest-selected-plan.md" in manifest

    assert "latest-plan.md" not in manifest
    assert "old-plan.md" not in manifest
    assert "latest-merged.patch" not in manifest
    assert "old-merged.patch" not in manifest
    assert "latest-fix-notes.md" not in manifest
    assert "latest-merge-report.md" not in manifest
    assert "latest-candidate.patch" not in manifest
    assert "latest-fix-candidate.patch" not in manifest
    assert "latest-bug-report.md" not in manifest
    assert "old-bug-report.md" not in manifest
    assert "latest-review-report.md" not in manifest
    assert "old-review-report.md" not in manifest
    assert "latest-decision.json" not in manifest
    assert "old-decision.json" not in manifest
    assert "final-round-2-test_gate.md" not in manifest
    assert "final-round-1-test_gate.md" not in manifest

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
    assert not (final_delivery.parent / "artifacts" / "merged_patch_metadata.md").exists()
    assert not (final_delivery.parent / "artifacts" / "merged_patch.diff").exists()
    assert not (final_delivery.parent / "artifacts" / "patch_validation.md").exists()
    assert not (final_delivery.parent / "artifacts" / "materialized_repo.md").exists()
    assert not (final_delivery.parent / "artifacts" / "merge_report.md").exists()
    assert not (final_delivery.parent / "artifacts" / "patch.diff").exists()
    assert (final_delivery.parent / "source" / "mock.txt").read_text(encoding="utf-8") == "mock change\n"
    merged_artifacts = orchestrator.repository.list_artifacts(task_id, "merged_patch.diff")
    validation_artifacts = orchestrator.repository.list_artifacts(task_id, "patch_validation.md")
    materialized_artifacts = orchestrator.repository.list_artifacts(task_id, "materialized_repo.md")
    success_path_artifacts = orchestrator.repository.list_artifacts(task_id, "success_path.md")
    assert merged_artifacts
    assert validation_artifacts
    assert "status: pass" in Path(validation_artifacts[-1]["path"]).read_text(encoding="utf-8")
    assert materialized_artifacts
    materialized_report = Path(materialized_artifacts[-1]["path"]).read_text(encoding="utf-8")
    assert "status: success" in materialized_report
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
    assert "patch_validation.md" not in manifest
    assert "materialized_repo.md" not in manifest
    assert "source/mock.txt" in manifest
    tester_run = next(
        run
        for run in orchestrator.repository.list_agent_runs(task_id)
        if run["role"] == "tester" and run["status"] == "COMPLETED"
    )
    tester_phase = next(
        phase for phase in orchestrator.repository.list_phases(task_id) if phase["phase_id"] == tester_run["phase_id"]
    )
    tester_repo = (
        Path(config["system"]["workspace_root"])
        / task_id
        / tester_run["phase_id"]
        / "tester"
        / tester_run["agent_id"]
        / f"round_{tester_phase['round_id']}"
        / f"attempt_{tester_run['retry_count']}"
        / "repo"
    )
    assert (tester_repo / "mock.txt").read_text(encoding="utf-8") == "mock change\n"

def test_delivery_internal_artifacts_are_explicit_opt_in(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["system"]["deliver_root"] = str(tmp_path / "deliver")
    config["delivery"] = {"include_internal_artifacts": True}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("Build Weather Tool With Audit Artifacts")

    final_delivery = orchestrator.run_task(task_id)

    artifact_dir = final_delivery.parent / "artifacts"
    assert (artifact_dir / "merged_patch_metadata.md").exists()
    assert (artifact_dir / "changed_files.md").exists()
    assert (artifact_dir / "self_check.md").exists()
    assert (artifact_dir / "review_report.md").exists()
    assert not (artifact_dir / "merged_patch.diff").exists()
    assert not (artifact_dir / "patch_validation.md").exists()
    assert not (artifact_dir / "materialized_repo.md").exists()
    manifest = (final_delivery.parent / "artifacts_manifest.md").read_text(encoding="utf-8")
    assert "artifacts/merged_patch_metadata.md" in manifest
    assert "patch_validation.md" not in manifest

def test_delivery_dependency_installer_infers_pytest_dependencies(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    project_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = project_dir / "source"
    (source_dir / "tests").mkdir(parents=True)
    (source_dir / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (source_dir / "src").mkdir()
    (source_dir / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (project_dir / "usage_guide.md").write_text(
        "```bash\npython3 -m pytest --cov=src tests/\n```\n",
        encoding="utf-8",
    )

    written = orchestrator._publish_dependency_installer(project_dir)

    requirements = source_dir / "requirements.txt"
    installer = source_dir / "install_dependencies.sh"
    assert requirements in written
    assert installer in written
    assert requirements.read_text(encoding="utf-8") == "pytest\npytest-cov\n"
    text = installer.read_text(encoding="utf-8")
    assert 'command -v python3' in text and '"$VENV_PYTHON" -m pip install -r requirements.txt' in text
    assert installer.stat().st_mode & 0o111

def test_delivery_dependency_installer_prefers_pyproject_dev_install(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    project_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\n[project.optional-dependencies]\ndev = [\"pytest\"]\n",
        encoding="utf-8",
    )

    written = orchestrator._publish_dependency_installer(project_dir)

    installer = source_dir / "install_dependencies.sh"
    assert written == [installer]
    assert '"$VENV_PYTHON" -m pip install -e ".[dev]"' in installer.read_text(encoding="utf-8")


def test_delivery_dependency_installer_uses_plain_editable_install_without_dev_extra(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    project_dir = tmp_path / "deliver" / "project-12345678"
    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")

    written = orchestrator._publish_dependency_installer(project_dir)

    installer = source_dir / "install_dependencies.sh"
    assert written == [installer]
    text = installer.read_text(encoding="utf-8")
    assert '"$VENV_PYTHON" -m pip install -e .' in text
    assert '".[dev]"' not in text


def test_delivery_project_name_uses_ascii_safe_slug(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["system"]["deliver_root"] = str(tmp_path / "deliver")
    orchestrator = Orchestrator(config)
    task_id = "859fe499-d655-455d-933d-34021a4aea67"

    assert orchestrator._slugify_project_name("做个双人对战的象棋游戏") == "project"
    assert orchestrator._slugify_project_name("做个 Chinese Chess Game!") == "chinese-chess-game"
    assert orchestrator._delivery_project_dir(task_id, "做个双人对战的象棋游戏").name == "project-859fe499"
