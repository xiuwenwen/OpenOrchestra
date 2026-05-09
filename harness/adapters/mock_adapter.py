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
        if context.role == "executor" and name == "response.md":
            return "artifact_result_code: 0\n\n# Response\n\nMock informational response for the user's request.\n"
        if context.role == "executor" and name == "notes.md":
            return "artifact_result_code: 0\n\n# Notes\n\nContext used: mock input artifacts.\nLimitations: mock adapter output.\n"
        if context.role == "reviewer" and name == "review_report.md":
            return "artifact_result_code: 0\n\n# Review Report\n\nreview_decision_code: 0\nNo changes required.\n"
        if context.role == "reviewer" and name == "selected_plan.md":
            return "artifact_result_code: 0\n\n# Selected Plan\n\nUse the merged mock planner proposal as the single execution plan.\n"
        if context.role == "planner" and name == "peer_review.md":
            return "artifact_result_code: 0\n\n# Peer Review\n\npeer_review_code: 1\nMock peer review requests one bounded revision loop.\n"
        if context.role == "communicator" and name == "final_delivery.md":
            return (
                "artifact_result_code: 0\n\n"
                "# Final Delivery\n\n"
                "final_delivery_code: 0\n\n"
                f"Task `{context.task_id}` completed through the mock harness flow.\n\n"
                "## Handoff\n\n"
                "- project_dir: source\n"
                "- run_command: python mock.py\n"
                "- dependency_install: none\n\n"
                "The orchestrator collected all required artifacts and received final judge approval.\n"
            )
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
                "- Confirm final judge approval exists.\n\n"
                "## Known Risks\n\n"
                "- This is mock output and does not contain a real implementation.\n"
            )
        if context.role == "judge" and name == "decision.json":
            return json.dumps(self._decision_payload(context), ensure_ascii=False, indent=2) + "\n"
        if context.role == "judge" and name == "decision_summary.md":
            payload = self._decision_payload(context)
            decision_code = -1 if payload["decision"] == "fail" else 1 if payload["decision"] == "changes_required" else 0
            return (
                "artifact_result_code: 0\n\n"
                "# Decision Summary\n\n"
                f"decision_code: {decision_code}\n"
                "decision_source: decision.json\n"
                f"Reason: {payload['reason']}\n"
            )
        if context.role == "executor" and name == "merged_patch_metadata.md":
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
        return (
            "artifact_result_code: 0\n\n"
            "# Merged Patch Metadata\n\n"
            "patch_artifact: merged_patch.diff\n"
            f"base_source_type: {source_type}\n"
            f"base_source_path: {source_path}\n"
            f"base_round: {context.round_id}\n"
            f"base_task_id: {context.task_id}\n"
            f"apply_target: {context.repo_dir}\n"
            "patch_scope: merged_authoritative\n"
            "changed_files: mock.txt\n"
            "selected_candidate_artifacts: patch.diff, fix_patch.diff\n"
            "expected_apply_command: git apply --check merged_patch.diff\n"
            "compatibility_notes: Mock merged patch metadata matches the current PATCH_MERGE workspace.\n"
        )

    def _decision_payload(self, context: AgentRunContext) -> dict[str, object]:
        if context.phase == PLAN_JUDGEMENT:
            return {"decision": "approved", "evidence": {}, "reason": "Mock plan is acceptable."}
        if context.phase == TEST_JUDGEMENT:
            return {"decision": "pass", "tests_passed": True, "evidence": {}, "reason": "Mock tests passed."}
        if context.phase == REVIEW_JUDGEMENT:
            return {"decision": "approved", "changes_required": False, "evidence": {}, "reason": "Mock review approved."}
        if context.phase == FINAL_JUDGEMENT:
            return {"decision": "approved", "final_approved": True, "evidence": {}, "reason": "Mock final approval granted."}
        return {"decision": "approved", "evidence": {}, "reason": "Mock judge approved."}
