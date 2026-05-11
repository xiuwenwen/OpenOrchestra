from __future__ import annotations

import json
from pathlib import Path

from harness.agents.result import ArtifactRef
from harness.diagnostics.service import DiagnosticsService
from harness.state.db import StateDB
from harness.state.repository import StateRepository


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
            "diagnostics_root": str(tmp_path / "diagnostics"),
        }
    }


def test_diagnostics_bundle_exports_state_logs_manifests_and_artifacts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = StateRepository(StateDB(config["system"]["state_db"]))
    task_id = repo.create_task("debug failed task with token=sk-testsecret12345")
    phase_id = repo.create_phase(task_id, "TESTING", "tester", 2)
    run_id = repo.create_agent_run(task_id, phase_id, "tester", "tester-1", 0)
    repo.update_agent_run_status(run_id, "OUTPUT_INVALID", "missing bug_report.md")
    repo.record_event(
        event_type="agent_failed",
        task_id=task_id,
        phase="TESTING",
        role="tester",
        agent_id="tester-1",
        round_id=2,
        attempt=0,
        status="OUTPUT_INVALID",
        message="api_key: sk-testsecret12345",
    )

    run_root = Path(config["system"]["workspace_root"]) / task_id / phase_id / "tester" / "tester-1" / "round_2" / "attempt_0"
    (run_root / "logs").mkdir(parents=True)
    (run_root / "input").mkdir(parents=True)
    (run_root / "logs" / "prompt.md").write_text("prompt with token=sk-testsecret12345", encoding="utf-8")
    (run_root / "logs" / "stdout.log").write_text("stdout", encoding="utf-8")
    (run_root / "logs" / "stderr.log").write_text("stderr password=secret-value", encoding="utf-8")
    (run_root / "input" / "manifest.md").write_text("# Input Artifacts\n", encoding="utf-8")

    artifact_path = tmp_path / "artifacts" / "bug_report.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("artifact_result_code: 0\napi_key: sk-testsecret12345\n", encoding="utf-8")
    repo.create_artifact(
        ArtifactRef(
            artifact_id=f"{task_id}-bug_report",
            task_id=task_id,
            phase_id=phase_id,
            role="tester",
            agent_id="tester-1",
            artifact_type="bug_report.md",
            path=artifact_path,
            version=1,
            hash=None,
        )
    )

    bundle = DiagnosticsService(config=config, repository=repo).export_task(task_id)

    assert bundle.path.exists()
    assert (bundle.path / "summary.md").exists()
    assert (bundle.path / "timeline.md").exists()
    assert json.loads((bundle.path / "state" / "task.json").read_text(encoding="utf-8"))["task_id"] == task_id
    assert list((bundle.path / "artifacts").glob("*bug_report.md"))
    assert list((bundle.path / "runs").rglob("prompt.md"))
    assert list((bundle.path / "runs").rglob("manifest.md"))
    all_text = "\n".join(path.read_text(encoding="utf-8") for path in bundle.path.rglob("*") if path.is_file())
    assert "sk-testsecret12345" not in all_text
    assert "secret-value" not in all_text
    assert "[REDACTED]" in all_text
