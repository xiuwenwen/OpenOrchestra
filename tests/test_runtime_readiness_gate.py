from __future__ import annotations

import uuid
import sys
from pathlib import Path

from harness.agents.result import ArtifactRef
from harness.core.orchestrator import Orchestrator
from harness.core.workflow_type import BUGFIX

from orchestrator_mock_support import _config


def test_runtime_readiness_gate_writes_structured_artifact(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["runtime_readiness"] = {"commands": [f"{sys.executable} -m compileall -q ."], "require_commands": True}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("review runtime", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_latest_materialized_repo", lambda task_id: repo)

    assert orchestrator._run_runtime_readiness_gate(task_id, 0)
    artifact = orchestrator.repository.list_artifacts(task_id, "runtime_readiness.md")[-1]
    report = Path(artifact["path"]).read_text(encoding="utf-8")

    assert "# Runtime Readiness Gate" in report
    assert "runtime: native" in report
    assert '"test_status": "pass"' in report


def test_reviewer_can_see_runtime_readiness_artifact(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("review runtime visibility", workflow_type=BUGFIX)
    readiness = tmp_path / "runtime_readiness.md"
    readiness.write_text(
        "# Runtime Readiness Gate\n\n"
        "status: pass\n"
        "round_id: 0\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=None,
            role="orchestrator",
            agent_id="runtime-readiness",
            artifact_type="runtime_readiness.md",
            path=readiness,
            version=1,
            hash="hash",
        )
    )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "reviewer", "REVIEWING", round_id=0)
    manifest = staged[0].read_text(encoding="utf-8")

    assert any(path.name.endswith("runtime_readiness.md") for path in staged[1:])
    assert "runtime_readiness.md" in manifest
