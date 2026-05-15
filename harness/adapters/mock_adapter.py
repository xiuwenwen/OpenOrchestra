from __future__ import annotations

import json
import time

from harness.adapters.base import AgentAdapter
from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult
from harness.core.state_machine import (
    FINAL_JUDGEMENT,
    PLAN_JUDGEMENT,
    REVIEW_JUDGEMENT,
    TEST_JUDGEMENT,
)
from harness.prompts.builder import PromptBuilder


class MockAgentAdapter(AgentAdapter):
    def __init__(self, prompt_builder: PromptBuilder | None = None):
        self.prompt_builder = prompt_builder or PromptBuilder()

    def run(self, context: AgentRunContext) -> AgentRunResult:
        delay_seconds = float(context.config.get("mock", {}).get("delay_seconds", 0.0))
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        context.output_dir.mkdir(parents=True, exist_ok=True)
        context.log_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = context.input_dir / "prompt.md"
        prompt_path.write_text(self.prompt_builder.build(context), encoding="utf-8")

        stdout_path = context.log_dir / "stdout.log"
        stderr_path = context.log_dir / "stderr.log"
        stdout_path.write_text(f"mock {context.role} completed {context.phase}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        for name in context.required_outputs:
            (context.output_dir / name).parent.mkdir(parents=True, exist_ok=True)
            (context.output_dir / name).write_text(self._content_for(context, name), encoding="utf-8")

        return AgentRunResult(
            task_id=context.task_id,
            phase_id=context.phase_id,
            role=context.role,
            agent_id=context.agent_id,
            status="COMPLETED",
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    def _content_for(self, context: AgentRunContext, name: str) -> str:
        if name == "delivery.md":
            return json.dumps(
                {
                    "return_code": 0,
                    "task_status": "success",
                    "role_return_code": 0,
                    "role": context.role,
                    "phase": context.phase,
                    "agent_id": context.agent_id,
                    "produced_files": list(context.required_outputs),
                    "known_risks": [],
                    "summary": "Mock agent completed the required output contract.",
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        if context.role == "tester" and name == "bug_report.md":
            return (
                "artifact_result_code: 0\n\n"
                "# Tester Report\n\n"
                "build_result_code: 0\n"
                "test_result_code: 0\n"
                "bug_result_code: 0\n\n"
                "Mock build and tests passed. No blocking bugs found.\n"
            )
        if context.role == "tester" and name == "tester_result.json":
            return json.dumps(
                {
                    "schema_version": 1,
                    "status": "tests_passed",
                    "next_action": "continue",
                    "failure_type": "none",
                    "environment_ready": True,
                    "environment_dependency_issue": False,
                    "summary": "Mock tester completed environment setup and verification.",
                    "setup_commands_run": [],
                    "test_commands_run": [
                        {
                            "command": "python -m compileall -q .",
                            "exit_code": 0,
                            "phase": "test",
                        }
                    ],
                    "oracle_results": [
                        {
                            "oracle_id": "A1",
                            "status": "passed",
                            "evidence": "Mock verification passed.",
                            "commands_run": ["python -m compileall -q ."],
                            "output_excerpt": "mock compile passed",
                        }
                    ],
                    "remaining_blockers": [],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        if context.role == "executor" and name == "response.md":
            return "artifact_result_code: 0\n\n# Response\n\nMock informational response for the user's request.\n"
        if context.role == "executor" and name == "notes.md":
            return "artifact_result_code: 0\n\n# Notes\n\nContext used: mock input artifacts.\nLimitations: mock adapter output.\n"
        if context.role == "reviewer" and name == "review_result.json":
            return json.dumps(
                {
                    "schema_version": 1,
                    "review_decision_code": 0,
                    "summary": "Mock reviewer approved the implementation.",
                    "findings": [],
                    "required_changes": [],
                    "environment_check": {
                        "attempted": True,
                        "status": "ready",
                        "commands_run": ["python mock.py"],
                        "fixable": True,
                        "blocking_reason": "",
                    },
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        if context.role == "reviewer" and name == "selected_plan.json":
            return json.dumps(
                {
                    "schema_version": 1,
                    "selected_plan_id": "mock-plan",
                    "summary": "Use the merged mock planner proposal as the single execution plan.",
                    "environment_contract_id": "mock-environment",
                    "validation_contract_id": "mock-validation",
                    "source_artifacts": ["plan.md", "todo_breakdown.json"],
                    "execution_order": ["implement mock change", "run mock verification"],
                    "acceptance_criteria": ["mock verification passes"],
                    "acceptance_oracles": [
                        {
                            "id": "A1",
                            "description": "Mock verification passes.",
                            "kind": "test",
                            "required": True,
                            "commands": ["python -m compileall -q ."],
                            "expected_exception": "",
                            "must_contain": [],
                            "must_not_contain": ["Traceback"],
                            "semantic_assertions": ["Mock verification reports no blocking bugs."],
                            "failure_signal": "Compile/check command fails or emits a traceback.",
                            "evidence_hint": "Record command exit code and relevant output excerpt.",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        if name in {"environment_contract_draft.json", "environment_contract.json"}:
            return json.dumps(
                {
                    "schema_version": "environment_contract.v1",
                    "contract_id": "mock-environment-draft" if name.endswith("_draft.json") else "mock-environment",
                    "contract_status": "draft" if name.endswith("_draft.json") else "final",
                    "source": "mock_adapter",
                    "confidence": "medium",
                    "runtime": {"type": "local", "language": "python", "version": "", "base_commit": "", "environment_setup_commit": ""},
                    "setup": {"mode": "none", "commands": [], "discovery_allowed": True, "notes": "Mock flow has no setup."},
                    "dependencies": {"mode": "none", "commands": [], "files": [], "notes": ""},
                    "constraints": {"forbidden_validation_methods": []},
                    "unknowns": [],
                    "evidence_sources": ["mock_adapter"],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        if name in {"validation_contract_draft.json", "validation_contract.json"}:
            return json.dumps(
                {
                    "schema_version": "validation_contract.v1",
                    "contract_id": "mock-validation-draft" if name.endswith("_draft.json") else "mock-validation",
                    "contract_status": "draft" if name.endswith("_draft.json") else "final",
                    "source": "mock_adapter",
                    "confidence": "medium",
                    "runtime": "local",
                    "tests": {
                        "mode": "explicit",
                        "commands": ["python -m compileall -q ."],
                        "discovery_allowed": True,
                        "fail_to_pass": [],
                        "pass_to_pass": [],
                        "notes": "Mock validation command.",
                    },
                    "pass_criteria": {"type": "commands_exit_zero", "conditions": ["python -m compileall -q . exits 0"], "resolved": None},
                    "acceptance_oracle_ids": ["A1"],
                    "unknowns": [],
                    "evidence_sources": ["mock_adapter"],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        if context.role == "planner" and name == "peer_review_result.json":
            return json.dumps(
                {
                    "schema_version": 1,
                    "peer_review_code": 1,
                    "peer_review_status": "changes_requested",
                    "summary": "Mock peer review requests one bounded revision loop.",
                    "findings": ["mock revision requested"],
                    "required_changes": ["revise once before plan review"],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        if context.role == "planner" and name == "todo_breakdown.json":
            return json.dumps(
                {
                    "schema_version": 1,
                    "todos": [
                        {
                            "id": "mock-1",
                            "title": "Implement mock change",
                            "owner_role": "executor",
                            "status": "pending",
                            "acceptance_criteria": ["mock verification passes"],
                        }
                    ],
                    "risks": [],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        if context.role == "communicator" and name == "final_delivery.json":
            return json.dumps(
                {
                    "schema_version": 1,
                    "final_delivery_code": 0,
                    "status": "delivered",
                    "summary": f"Task {context.task_id} completed through the mock harness flow.",
                    "delivered_artifacts": ["source", "usage_guide.md"],
                    "verification": ["Every role delivery.md reports return_code 0."],
                    "known_risks": ["This is mock output and does not contain a real implementation."],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        if context.role == "communicator" and name == "usage_guide.md":
            return (
                "artifact_result_code: 0\n\n"
                "# Usage Guide\n\n"
                "## Prerequisites\n\n"
                "- Use the collected artifacts for this task.\n\n"
                "## How To Use The Delivery\n\n"
                "```bash\n"
                "python mock.py\n"
                "```\n\n"
                "## Verification\n\n"
                "- Confirm every role delivery.md reports `return_code: 0`.\n"
                "- Confirm the delivered project runs with the documented command.\n\n"
                "## Known Risks\n\n"
                "- This is mock output and does not contain a real implementation.\n"
            )
        if context.role == "judge" and name == "decision.json":
            return json.dumps(self._decision_payload(context), ensure_ascii=False, indent=2) + "\n"
        if context.role == "executor" and name == "merged_patch_metadata.json":
            return self._merged_patch_metadata(context)
        if name.endswith(".diff"):
            return "diff --git a/mock.txt b/mock.txt\nnew file mode 100644\n--- /dev/null\n+++ b/mock.txt\n@@ -0,0 +1 @@\n+mock change\n"
        title = name.replace("_", " ").replace(".md", "").title()
        if name.endswith(".md"):
            return f"artifact_result_code: 0\n\n# {title}\n\nMock output for role `{context.role}` in phase `{context.phase}`.\n"
        return f"# {title}\n\nMock output for role `{context.role}` in phase `{context.phase}`.\n"

    def _merged_patch_metadata(self, context: AgentRunContext) -> str:
        source_type = context.config.get("repository_source_type") or context.config.get("project_context", {}).get(
            "repository_source_type", "mock_repo"
        )
        source_path = context.config.get("repository_source_path") or context.config.get("project_context", {}).get(
            "repository_source_path", str(context.repo_dir)
        )
        return json.dumps(
            {
                "schema_version": 1,
                "patch_artifact": "merged_patch.diff",
                "base_source_type": source_type,
                "base_source_path": str(source_path),
                "round_id": context.round_id,
                "base_round": context.round_id,
                "base_task_id": context.task_id,
                "apply_target": str(context.repo_dir),
                "patch_scope": "merged_authoritative",
                "changed_files": ["mock.txt"],
                "selected_candidate_artifacts": ["patch.diff", "fix_patch.diff"],
                "expected_apply_command": "git apply --check merged_patch.diff",
                "compatibility_notes": "Mock merged patch metadata matches the current PATCH_MERGE workspace.",
                "merge_report": {
                    "merge_strategy": "mock_merge",
                    "selected_candidate_artifacts": ["patch.diff", "fix_patch.diff"],
                    "rejected_candidate_artifacts": [],
                    "conflict_handling": "not_applicable",
                    "ready_for_testing": "yes",
                },
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n"

    def _decision_payload(self, context: AgentRunContext) -> dict[str, object]:
        if context.phase == PLAN_JUDGEMENT:
            return {
                "schema_version": 1,
                "decision_code": 0,
                "decision": "approved",
                "summary": "Mock plan is acceptable.",
                "evidence": [],
                "reason": "Mock plan is acceptable.",
            }
        if context.phase == TEST_JUDGEMENT:
            return {
                "schema_version": 1,
                "decision_code": 0,
                "decision": "pass",
                "tests_passed": True,
                "summary": "Mock tests passed.",
                "evidence": [],
                "reason": "Mock tests passed.",
            }
        if context.phase == REVIEW_JUDGEMENT:
            return {
                "schema_version": 1,
                "decision_code": 0,
                "decision": "approved",
                "changes_required": False,
                "summary": "Mock review approved.",
                "evidence": [],
                "reason": "Mock review approved.",
            }
        if context.phase == FINAL_JUDGEMENT:
            return {
                "schema_version": 1,
                "decision_code": 0,
                "decision": "approved",
                "final_approved": True,
                "summary": "Mock final approval granted.",
                "evidence": [],
                "reason": "Mock final approval granted.",
            }
        return {
            "schema_version": 1,
            "decision_code": 0,
            "decision": "approved",
            "summary": "Mock judge approved.",
            "evidence": [],
            "reason": "Mock judge approved.",
        }
