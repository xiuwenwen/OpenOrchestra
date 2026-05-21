from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

from harness.adapters.command_runner import CapturedCommandResult
from harness.agents.result import ArtifactRef
from harness.core.orchestrator import Orchestrator
from harness.core.state_machine import REVIEW_FIXING
from harness.core.workflow_type import BUGFIX
from orchestrator_mock_support import _config


def _validation_contract(command: str) -> str:
    return (
        json.dumps(
            {
                "schema_version": "validation_contract.v1",
                "contract_id": "validation-final",
                "contract_status": "final",
                "source": "plan_review",
                "confidence": "high",
                "runtime": "native",
                "tests": {"mode": "unknown", "commands": [], "discovery_allowed": True},
                "pass_criteria": {"type": "commands_exit_zero", "conditions": [], "resolved": None},
                "final_check": {
                    "mode": "explicit",
                    "authority": "external evaluator",
                    "commands": [command],
                    "failure_type": "source_bug",
                },
                "acceptance_oracle_ids": [],
                "unknowns": [],
                "evidence_sources": [],
            }
        )
        + "\n"
    )

def test_final_validation_gate_writes_external_evaluator_result(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["runtime"] = {"mode": "host"}
    config["final_validation"] = {"allow_contract_commands": True}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("run final evaluator", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    orchestrator.final_validation_gate_service.latest_materialized_repo = lambda task_id: repo
    orchestrator.artifact_manager.create_text_artifact(
        task_id,
        "validation_contract.json",
        _validation_contract(f"{sys.executable} -c \"print('ok')\""),
        role="reviewer",
        agent_id="reviewer-1",
    )

    result = orchestrator.final_validation_gate_service.run(task_id, 0)

    assert result.passed
    artifact = orchestrator.repository.list_artifacts(task_id, "external_evaluator_result.json")[-1]
    payload = json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["failure_type"] == "none"
    assert payload["commands"][0]["exit_code"] == 0


def test_final_validation_blocks_contract_commands_by_default(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["runtime"] = {"mode": "host"}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("contract command should not be self-authorizing", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    orchestrator.final_validation_gate_service.latest_materialized_repo = lambda task_id: repo
    orchestrator.artifact_manager.create_text_artifact(
        task_id,
        "validation_contract.json",
        _validation_contract(f"{sys.executable} -c \"print('ok')\""),
        role="reviewer",
        agent_id="reviewer-1",
    )

    result = orchestrator.final_validation_gate_service.run(task_id, 0)

    assert result.failed
    artifact = orchestrator.repository.list_artifacts(task_id, "external_evaluator_result.json")[-1]
    payload = json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["failure_type"] == "contract_bug"
    assert "not Harness-authorized" in payload["summary"]


def test_final_validation_uses_resolved_runtime_and_container_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["runtime"] = {
        "mode": "docker",
        "docker": {"image": "openorchestra/runtime:test", "workdir": "/workspace", "network": "none"},
    }
    config["final_validation"] = {"allow_contract_commands": True}
    orchestrator = Orchestrator(config)
    task_id = orchestrator.create_task("run docker final evaluator", workflow_type=BUGFIX)
    repo = tmp_path / "repo"
    repo.mkdir()
    orchestrator.final_validation_gate_service.latest_materialized_repo = lambda task_id: repo
    orchestrator.artifact_manager.create_text_artifact(
        task_id,
        "validation_contract.json",
        _validation_contract("python check.py --repo {repo_dir} --logs {external_evaluator_log_dir}"),
        role="reviewer",
        agent_id="reviewer-1",
    )
    calls: list[dict[str, object]] = []

    class FakeRunner:
        def run_capture(self, command, cwd, timeout_seconds=None, input_text=None, env=None, runtime_spec=None):
            calls.append({"command": command, "cwd": cwd, "runtime_spec": runtime_spec})
            return CapturedCommandResult(returncode=0, stdout="ok\n", stderr="")

    orchestrator.final_validation_gate_service.command_runner = FakeRunner()

    result = orchestrator.final_validation_gate_service.run(task_id, 0)

    assert result.passed
    assert calls[0]["cwd"] == repo
    runtime_spec = calls[0]["runtime_spec"]
    assert runtime_spec is not None and runtime_spec.is_docker
    assert calls[0]["command"] == [
        "python",
        "check.py",
        "--repo",
        "/workspace",
        "--logs",
        "/openorchestra/external_evaluator_logs",
    ]
    artifact = orchestrator.repository.list_artifacts(task_id, "external_evaluator_result.json")[-1]
    payload = json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))
    assert payload["runtime"]["mode"] == "docker"
    assert payload["runtime"]["repo_dir"] == "/workspace"
    assert payload["commands"][0]["runtime"]["image"] == "openorchestra/runtime:test"


def test_executor_can_see_external_evaluator_result(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("external evaluator visibility", workflow_type=BUGFIX)
    result_path = tmp_path / "external_evaluator_result.json"
    result_path.write_text(
        json.dumps({"status": "failed", "failure_type": "source_bug", "summary": "official evaluator failed"}) + "\n",
        encoding="utf-8",
    )
    orchestrator.repository.create_artifact(
        ArtifactRef(
            artifact_id=str(uuid.uuid4()),
            task_id=task_id,
            phase_id=None,
            role="orchestrator",
            agent_id="external-evaluator",
            artifact_type="external_evaluator_result.json",
            path=result_path,
            version=1,
            hash="hash",
        )
    )

    staged = orchestrator._stage_input_artifacts(task_id, tmp_path / "input", "executor", REVIEW_FIXING, round_id=1)
    manifest = staged[0].read_text(encoding="utf-8")

    assert any(path.name.endswith("external_evaluator_result.json") for path in staged[1:])
    assert "external_evaluator_result.json" in manifest


def test_final_validation_failure_routes_to_executor_review_fixing(monkeypatch, tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("final evaluator failure", workflow_type=BUGFIX)
    results = [
        SimpleNamespace(passed=False, skipped=False, failure_type="source_bug", summary="official evaluator failed"),
        SimpleNamespace(passed=True, skipped=False, failure_type="none", summary="passed"),
    ]
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(orchestrator, "run_final_validation_gate", lambda *args, **kwargs: results.pop(0))
    monkeypatch.setattr(orchestrator, "run_role_phase", lambda role, phase, *args, **kwargs: calls.append((role, phase)) or [])
    monkeypatch.setattr(orchestrator, "run_patch_merge", lambda *args, **kwargs: True)
    monkeypatch.setattr(orchestrator.workflow_engine, "run_regression_test_fix_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator.workflow_engine, "run_review_loop", lambda *args, **kwargs: None)

    orchestrator.workflow_engine.run_final_validation_loop(task_id, "fix it")

    assert ("executor", REVIEW_FIXING) in calls
