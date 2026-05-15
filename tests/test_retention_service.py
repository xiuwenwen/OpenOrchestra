from __future__ import annotations

from pathlib import Path

from harness.agents.result import ArtifactRef
from harness.retention.service import RetentionService
from harness.state.db import StateDB
from harness.state.repository import StateRepository


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
        }
    }


def _artifact(task_id: str, artifact_type: str, path: Path) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"{task_id}-{artifact_type}",
        task_id=task_id,
        phase_id=None,
        role="orchestrator",
        agent_id="harness",
        artifact_type=artifact_type,
        path=path,
        version=1,
        hash=None,
    )


def test_retention_service_dry_run_keeps_files_and_reports_freed_bytes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    task_id = repo.create_task("clean delivered task")
    success_dir = tmp_path / "deliver" / "project"
    success_dir.mkdir(parents=True)
    success_path = success_dir / "success_path.md"
    success_path.write_text(f"success_path: {success_dir}\n", encoding="utf-8")
    final_delivery = success_dir / "final_delivery.json"
    final_delivery.write_text("done", encoding="utf-8")
    repo.create_artifact(_artifact(task_id, "success_path.md", success_path))
    workspace_dir = Path(config["system"]["workspace_root"]) / task_id
    artifact_dir = Path(config["system"]["artifact_root"]) / task_id
    workspace_dir.mkdir(parents=True)
    artifact_dir.mkdir(parents=True)
    (workspace_dir / "file.txt").write_text("workspace", encoding="utf-8")
    (artifact_dir / "artifact.txt").write_text("artifact", encoding="utf-8")

    plan = RetentionService(config=config, repository=repo).clean_task(task_id, dry_run=True)

    assert plan.dry_run is True
    assert plan.freed_bytes > 0
    assert workspace_dir.exists()
    assert artifact_dir.exists()
    assert final_delivery.exists()


def test_retention_service_deletes_intermediate_dirs_and_keeps_delivery(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    task_id = repo.create_task("clean delivered task")
    success_dir = tmp_path / "deliver" / "project"
    success_dir.mkdir(parents=True)
    final_delivery = success_dir / "final_delivery.json"
    final_delivery.write_text("done", encoding="utf-8")
    repo.create_artifact(_artifact(task_id, "final_delivery.json", final_delivery))
    workspace_dir = Path(config["system"]["workspace_root"]) / task_id
    artifact_dir = Path(config["system"]["artifact_root"]) / task_id
    workspace_dir.mkdir(parents=True)
    artifact_dir.mkdir(parents=True)
    (workspace_dir / "file.txt").write_text("workspace", encoding="utf-8")
    (artifact_dir / "artifact.txt").write_text("artifact", encoding="utf-8")

    plan = RetentionService(config=config, repository=repo).clean_task(task_id)

    assert len(plan.deleted) == 2
    assert not workspace_dir.exists()
    assert not artifact_dir.exists()
    assert final_delivery.exists()


def test_retention_service_refuses_active_task(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    task_id = repo.create_task("running task")
    repo.update_task(task_id, status="RUNNING")
    workspace_dir = Path(config["system"]["workspace_root"]) / task_id
    workspace_dir.mkdir(parents=True)

    plan = RetentionService(config=config, repository=repo).clean_task(task_id)

    assert not plan.deleted
    assert plan.skipped[0].reason == "task is active"
    assert workspace_dir.exists()
