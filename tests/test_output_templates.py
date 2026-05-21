from __future__ import annotations

import json
from pathlib import Path

from harness.agents.result import AgentRunResult
from harness.artifacts.output_templates import (
    TEMPLATE_PENDING_VALUE,
    output_has_pending_template_marker,
    seed_output_templates,
)
from harness.artifacts.schemas import required_outputs_for
from harness.artifacts.validator import ArtifactValidator, delivery_issue_is_contract_only
from harness.core.orchestrator import Orchestrator
from harness.core.state_machine import FIXING, PLANNING_DRAFT


def _config(tmp_path: Path) -> dict:
    return {
        "system": {
            "workspace_root": str(tmp_path / "workspaces"),
            "artifact_root": str(tmp_path / "artifacts"),
            "deliver_root": str(tmp_path / "deliver"),
            "state_db": str(tmp_path / "state" / "harness.db"),
        },
        "agent_backend": {"default": "mock", "planner": "mock"},
        "roles": {"planner": {"count": 1}},
        "limits": {"max_agent_retry": 0},
        "timeouts": {"planner": 5},
        "policy": {"same_role_can_run_concurrently": True},
    }


def test_seed_output_templates_creates_non_diff_templates_only(tmp_path: Path) -> None:
    required_outputs = required_outputs_for("executor", FIXING)

    seeded = seed_output_templates(
        tmp_path,
        required_outputs,
        role="executor",
        phase=FIXING,
        agent_id="executor-1",
    )

    seeded_names = {path.name for path in seeded}
    assert "fix_schedule.md" in seeded_names
    assert "fix_notes.md" in seeded_names
    assert "self_check.md" in seeded_names
    assert "delivery.md" in seeded_names
    assert "fix_patch.diff" not in seeded_names
    assert not (tmp_path / "fix_patch.diff").exists()
    assert "artifact_result_code: 0" in (tmp_path / "fix_schedule.md").read_text(encoding="utf-8")
    assert output_has_pending_template_marker(tmp_path / "fix_schedule.md")

    delivery = json.loads((tmp_path / "delivery.md").read_text(encoding="utf-8"))
    assert delivery["return_code"] == 0
    assert delivery["harness_template_status"] == TEMPLATE_PENDING_VALUE


def test_validator_rejects_uncompleted_output_templates(tmp_path: Path) -> None:
    seed_output_templates(
        tmp_path,
        ["plan.md", "delivery.md"],
        role="planner",
        phase=PLANNING_DRAFT,
        agent_id="planner-1",
    )

    result = ArtifactValidator().validate_required_outputs_result(tmp_path, ["plan.md", "delivery.md"])

    assert not result.ok
    assert result.errors == [
        "plan.md still contains Harness output template marker",
        "delivery.md still contains Harness output template marker",
    ]


def test_delivery_template_marker_is_delivery_contract_issue(tmp_path: Path) -> None:
    seed_output_templates(
        tmp_path,
        ["delivery.md"],
        role="planner",
        phase=PLANNING_DRAFT,
        agent_id="planner-1",
    )

    result = ArtifactValidator().validate_required_outputs_result(tmp_path, ["delivery.md"])

    assert not result.ok
    assert delivery_issue_is_contract_only(result)


def test_review_result_template_is_strict_json_and_schema_validated(tmp_path: Path) -> None:
    seed_output_templates(tmp_path, ["review_result.json"], role="reviewer", phase="REVIEWING", agent_id="reviewer-1")
    review_result_path = tmp_path / "review_result.json"
    payload = json.loads(review_result_path.read_text(encoding="utf-8"))
    assert payload["review_decision_code"] == TEMPLATE_PENDING_VALUE
    assert payload["review_decision_code_meaning"]["2"] == "blocked"
    assert "review_status" not in payload
    assert output_has_pending_template_marker(review_result_path)

    payload.pop("harness_template_status")
    payload.update(
        {
            "review_decision_code": 0,
            "review_status": "approved",
            "environment_check": {"attempted": True, "status": "ready", "commands_run": ["pytest"], "fixable": True, "blocking_reason": ""},
        }
    )
    review_result_path.write_text(json.dumps(payload), encoding="utf-8")

    result = ArtifactValidator().validate_required_outputs_result(tmp_path, ["review_result.json"])
    assert (result.ok, result.errors) == (
        False,
        ["review_result.json review_status is deprecated; route only with review_decision_code"],
    )


def test_selected_plan_template_contains_acceptance_oracles(tmp_path: Path) -> None:
    seed_output_templates(
        tmp_path,
        ["selected_plan.json"],
        role="reviewer",
        phase="PLAN_REVIEW",
        agent_id="reviewer-1",
    )

    payload = json.loads((tmp_path / "selected_plan.json").read_text(encoding="utf-8"))

    assert payload["acceptance_oracles"][0]["id"] == TEMPLATE_PENDING_VALUE
    assert payload["acceptance_oracles"][0]["verification_mode_code"] == TEMPLATE_PENDING_VALUE
    assert payload["acceptance_oracles"][0]["required"] is True
    assert payload["reviewer_integrated_findings"] == []
    assert payload["required_executor_notes"] == []


def test_tester_result_template_contains_oracle_results(tmp_path: Path) -> None:
    seed_output_templates(
        tmp_path,
        ["tester_result.json"],
        role="tester",
        phase="TESTING",
        agent_id="tester-1",
    )

    payload = json.loads((tmp_path / "tester_result.json").read_text(encoding="utf-8"))

    assert payload["oracle_results"][0]["oracle_id"] == TEMPLATE_PENDING_VALUE
    assert payload["tester_status_code"] == TEMPLATE_PENDING_VALUE
    assert payload["next_action_code"] == TEMPLATE_PENDING_VALUE
    assert payload["oracle_results"][0]["oracle_result_code"] == TEMPLATE_PENDING_VALUE
    assert "commands_run" in payload["oracle_results"][0]


def test_contract_templates_use_mode_not_bare_empty_commands(tmp_path: Path) -> None:
    seed_output_templates(
        tmp_path,
        ["environment_contract_draft.json", "validation_contract_draft.json"],
        role="planner",
        phase="PLANNING_DRAFT",
        agent_id="planner-1",
    )

    environment = json.loads((tmp_path / "environment_contract_draft.json").read_text(encoding="utf-8"))
    validation = json.loads((tmp_path / "validation_contract_draft.json").read_text(encoding="utf-8"))

    assert environment["setup"]["mode"] == TEMPLATE_PENDING_VALUE
    assert environment["setup"]["commands"] == []
    assert validation["tests"]["mode"] == TEMPLATE_PENDING_VALUE
    assert validation["tests"]["commands"] == []
    assert validation["final_check"]["mode"] == "unknown"
    assert validation["final_check"]["commands"] == []
    assert validation["final_check"]["failure_type"] == "source_bug"


def test_runner_seeds_output_templates_before_adapter_invocation(tmp_path: Path, monkeypatch) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    task_id = orchestrator.create_task("plan with seeded templates")
    required_outputs = required_outputs_for("planner", PLANNING_DRAFT)
    observed: dict[str, bool] = {}

    class TemplateAwareAdapter:
        def run(self, context):
            observed["plan_template_exists"] = (context.output_dir / "plan.md").exists()
            observed["plan_template_pending"] = output_has_pending_template_marker(context.output_dir / "plan.md")
            observed["delivery_template_exists"] = (context.output_dir / "delivery.md").exists()
            for name in context.required_outputs:
                path = context.output_dir / name
                if name == "delivery.md":
                    path.write_text(
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
                elif name == "todo_breakdown.json":
                    path.write_text(
                        json.dumps({"schema_version": 1, "todos": [], "risks": []}) + "\n",
                        encoding="utf-8",
                    )
                elif name == "environment_contract_draft.json":
                    path.write_text(
                        json.dumps(
                            {
                                "schema_version": "environment_contract.v1",
                                "contract_id": "env-draft",
                                "contract_status": "draft",
                                "source": "test",
                                "confidence": "unknown",
                                "runtime": {"type": "unknown"},
                                "setup": {"mode": "unknown", "commands": [], "discovery_allowed": True},
                                "dependencies": {"mode": "unknown", "commands": [], "files": []},
                                "unknowns": ["test fixture"],
                                "evidence_sources": [],
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                elif name == "validation_contract_draft.json":
                    path.write_text(
                        json.dumps(
                            {
                                "schema_version": "validation_contract.v1",
                                "contract_id": "validation-draft",
                                "contract_status": "draft",
                                "source": "test",
                                "confidence": "unknown",
                                "runtime": "unknown",
                                "tests": {"mode": "unknown", "commands": [], "discovery_allowed": True},
                                "pass_criteria": {"type": "unknown", "conditions": []},
                                "acceptance_oracle_ids": [],
                                "unknowns": ["test fixture"],
                                "evidence_sources": [],
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                else:
                    path.write_text(f"artifact_result_code: 0\n\n# {name}\n\nCompleted.\n", encoding="utf-8")
            stdout = context.log_dir / "stdout.log"
            stderr = context.log_dir / "stderr.log"
            stdout.write_text("ok\n", encoding="utf-8")
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

    monkeypatch.setattr(orchestrator, "_adapter_for_backend", lambda backend: TemplateAwareAdapter())

    orchestrator.run_role_phase("planner", PLANNING_DRAFT, 0, required_outputs, "plan with seeded templates")

    assert observed == {
        "plan_template_exists": True,
        "plan_template_pending": True,
        "delivery_template_exists": True,
    }
    artifacts = orchestrator.repository.list_artifacts(task_id, "plan.md")
    assert artifacts
