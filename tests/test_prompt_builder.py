from __future__ import annotations

import re
from pathlib import Path

from harness.agents.context import AgentRunContext
from harness.artifacts.schemas import required_outputs_for
from harness.prompts.builder import PromptBuilder


def make_context(tmp_path: Path, *, role: str, agent_id: str, role_count: int) -> AgentRunContext:
    return AgentRunContext(
        task_id="task-1",
        phase_id="phase-1",
        phase="PLANNING_DRAFT" if role == "planner" else "TESTING",
        role=role,
        agent_id=agent_id,
        round_id=0,
        user_prompt="Build a small weather application.",
        role_instruction=f"Act as {role}.",
        workspace_dir=tmp_path / "workspace",
        repo_dir=tmp_path / "workspace" / "repo",
        input_dir=tmp_path / "workspace" / "input",
        output_dir=tmp_path / "workspace" / "output",
        log_dir=tmp_path / "workspace" / "logs",
        input_artifacts=[],
        required_outputs=["delivery.md"],
        timeout_seconds=30,
        config={"roles": {role: {"count": role_count}}},
    )


def test_prompt_builder_outputs_precise_english_contract(tmp_path: Path) -> None:
    context = AgentRunContext(
        task_id="task-1",
        phase_id="phase-1",
        phase="EXECUTION",
        role="executor",
        agent_id="executor-1",
        round_id=0,
        user_prompt="Build a small weather application.",
        role_instruction="Produce implementation artifacts only.",
        workspace_dir=tmp_path / "workspace",
        repo_dir=tmp_path / "workspace" / "repo",
        input_dir=tmp_path / "workspace" / "input",
        output_dir=tmp_path / "workspace" / "output",
        log_dir=tmp_path / "workspace" / "logs",
        input_artifacts=[],
        required_outputs=["implementation_plan.md", "changed_files.md", "patch.diff", "self_check.md", "delivery.md"],
        timeout_seconds=30,
        config={},
    )

    prompt = PromptBuilder().build(context)

    assert "# Harness Agent Contract" in prompt
    assert "## Role Boundary" in prompt
    assert "Source access: writable for implementation phases" in prompt
    assert "## Output Contract" in prompt
    assert "## Required Output Paths" in prompt
    assert f"`patch.diff`: `{tmp_path / 'workspace' / 'output' / 'patch.diff'}`" in prompt
    assert "A similarly named file under any other path is invalid." in prompt
    assert "## Prohibited Actions" in prompt
    assert "valid git-style unified diff" in prompt
    assert "git add -N . && git diff --no-ext-diff" in prompt
    assert "Do not paste a large patch into a Write-tool payload" in prompt
    assert not re.search(r"[\u4e00-\u9fff]", prompt)


def test_prompt_builder_marks_merged_patch_as_authoritative(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="tester", agent_id="tester-1", role_count=1)

    prompt = PromptBuilder().build(context)

    assert "## Test Target" in prompt
    assert "Test this exact repository directory" in prompt
    assert "## Required Test Work" in prompt
    assert "Record exact commands run, exit codes" in prompt
    assert "There is no post-tester Harness test gate" in prompt
    assert "project-declared dependency commands" in prompt
    assert "Do not install arbitrary third-party packages" in prompt
    assert "## Tester Output" in prompt
    assert "Treat the repository directory as the implementation under test" in prompt
    assert "`tester_result.json` must be exactly one JSON object" in prompt
    assert "tester_result.json.oracle_results" in prompt
    assert "environment_dependency_issue" in prompt
    assert "Do not treat executor planning notes, self-checks, or change summaries as test evidence" in prompt


def test_prompt_builder_hides_generic_error_code_tables_from_tester(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="tester", agent_id="tester-1", role_count=1)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "required_outputs": required_outputs_for("tester", "TESTING"),
        }
    )

    prompt = PromptBuilder().build(context)

    assert "Return code meanings:" not in prompt
    assert "Markdown artifact result code meanings:" not in prompt
    assert "`bug_report.md` must contain `artifact_result_code: 0` somewhere in the file" in prompt
    assert "tester_result.json.tester_status_code" in prompt
    assert "single tester report" in prompt
    assert "line 1 must be `artifact_result_code: 0`" not in prompt
    assert "put headings such as `# Report` only after that line" not in prompt
    assert "Do not copy `build_result_code`, `test_result_code`, or `bug_result_code` values" in prompt
    assert "build_result_code: -1" in prompt
    assert "test_result_code: 2" in prompt
    assert "bug_result_code: -1" in prompt
    assert "`artifact_result_code: -1`" not in prompt
    assert "`artifact_result_code: 2`" not in prompt


def test_prompt_builder_has_patch_merge_contract(tmp_path: Path) -> None:
    context = AgentRunContext(
        task_id="task-1",
        phase_id="phase-1",
        phase="PATCH_MERGE",
        role="executor",
        agent_id="executor-1",
        round_id=0,
        user_prompt="Build a small weather application.",
        role_instruction="Merge candidate implementation patches.",
        workspace_dir=tmp_path / "workspace",
        repo_dir=tmp_path / "workspace" / "repo",
        input_dir=tmp_path / "workspace" / "input",
        output_dir=tmp_path / "workspace" / "output",
        log_dir=tmp_path / "workspace" / "logs",
        input_artifacts=[],
        required_outputs=required_outputs_for("executor", "PATCH_MERGE"),
        timeout_seconds=30,
        config={"roles": {"executor": {"count": 1}}},
    )

    prompt = PromptBuilder().build(context)

    assert "PATCH_MERGE phase" in prompt
    assert "Do not concatenate blindly" in prompt
    assert "exactly one authoritative `merged_patch.diff`" in prompt
    assert "do not paste a large merged diff as a Write-tool payload" in prompt
    assert "`patch_metadata.md`" not in prompt
    assert "`merged_patch_metadata.json`" in prompt
    assert "`diff --git` file headers" in prompt
    assert "Do not select a patch based only on filename" in prompt
    assert "Prior `merged_patch.diff` artifacts are historical evidence" in prompt
    assert "## Role Specialization" not in prompt
    assert "No additional role specialization" not in prompt


def test_prompt_builder_keeps_role_specialization_when_configured(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="planner", agent_id="planner-1", role_count=1)

    prompt = PromptBuilder().build(context)

    assert "## Role Specialization" in prompt
    assert "Specialization:" in prompt


def test_executor_patch_outputs_do_not_require_candidate_patch_metadata(tmp_path: Path) -> None:
    context = AgentRunContext(
        task_id="task-1",
        phase_id="phase-1",
        phase="FIXING",
        role="executor",
        agent_id="executor-1",
        round_id=2,
        user_prompt="Fix the failing scheduler test.",
        role_instruction="Produce a fix patch.",
        workspace_dir=tmp_path / "workspace",
        repo_dir=tmp_path / "workspace" / "repo",
        input_dir=tmp_path / "workspace" / "input",
        output_dir=tmp_path / "workspace" / "output",
        log_dir=tmp_path / "workspace" / "logs",
        input_artifacts=[],
        required_outputs=required_outputs_for("executor", "FIXING"),
        timeout_seconds=30,
        config={},
    )

    prompt = PromptBuilder().build(context)

    assert "`patch_metadata.md`" not in prompt
    assert "Produce `patch_metadata.md` next to the patch" not in prompt
    assert "target the current materialized/source repository" in prompt
    assert "historical empty project baseline" in prompt


def test_tester_prompt_requires_setup_before_reporting_blocked(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="tester", agent_id="tester-1", role_count=1)

    prompt = PromptBuilder().build(context)

    assert "run safe project-declared setup/install commands" in prompt
    assert "missing dependencies" in prompt
    assert "rerun the relevant build/test command" in prompt


def test_prompt_builder_uses_balanced_planner_when_count_is_one(tmp_path: Path) -> None:
    prompt = PromptBuilder().build(make_context(tmp_path, role="planner", agent_id="planner-1", role_count=1))

    assert "Specialization: Balanced Planner." in prompt
    assert "Balanced planning, covering MVP feasibility" in prompt
    assert "Define clear validation criteria for Tester." in prompt
    assert "`todo_breakdown.json` must be exactly one JSON object" in prompt
    assert "environment_contract_draft.json" in prompt
    assert "validation_contract_draft.json" in prompt
    assert 'mode: "unknown"' in prompt
    assert '"todos":[{"id":"T1"' in prompt
    assert '"acceptance_criteria":[]' in prompt
    assert '"test_commands":[]' in prompt


def test_prompt_builder_uses_fix_modify_planner_profiles_for_existing_project_workflows(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="planner", agent_id="planner-1", role_count=2)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "metadata": {"workflow_type": "feature_change"},
        }
    )

    prompt = PromptBuilder().build(context)

    assert "Specialization: Minimal Patch Planner." in prompt
    assert "Task Type Classification" in prompt or "Classify the task" in prompt
    assert "files/modules likely to change and files/modules that should not change" in prompt
    assert "Pragmatic Planner" not in prompt


def test_prompt_builder_uses_distinct_bugfix_planner_profiles(tmp_path: Path) -> None:
    builder = PromptBuilder()
    base = make_context(tmp_path, role="planner", agent_id="planner-1", role_count=2)
    planner_1 = builder.build(
        AgentRunContext(
            **{
                **base.__dict__,
                "agent_id": "planner-1",
                "metadata": {"workflow_type": "bugfix"},
            }
        )
    )
    planner_2 = builder.build(
        AgentRunContext(
            **{
                **base.__dict__,
                "agent_id": "planner-2",
                "metadata": {"workflow_type": "bugfix"},
            }
        )
    )

    assert "Specialization: Root Cause Minimal Fix Planner." in planner_1
    assert "not just the first error message" in planner_1
    assert "Specialization: Verification & Regression Bugfix Planner." in planner_2
    assert "runnable tests or smoke checks must be run" in planner_2
    assert planner_1 != planner_2


def test_prompt_builder_specializes_two_planners(tmp_path: Path) -> None:
    builder = PromptBuilder()

    planner_1 = builder.build(make_context(tmp_path, role="planner", agent_id="planner-1", role_count=2))
    planner_2 = builder.build(make_context(tmp_path, role="planner", agent_id="planner-2", role_count=2))

    assert "Specialization: Pragmatic Planner." in planner_1
    assert "Identify what can be mocked." in planner_1
    assert "Specialization: Robust Planner." in planner_2
    assert "Ensure artifact traceability and auditability." in planner_2


def test_prompt_builder_specializes_four_testers(tmp_path: Path) -> None:
    builder = PromptBuilder()

    tester_1 = builder.build(make_context(tmp_path, role="tester", agent_id="tester-1", role_count=4))
    tester_4 = builder.build(make_context(tmp_path, role="tester", agent_id="tester-4", role_count=4))

    assert "Specialization: Build & Smoke Tester." in tester_1
    assert "Mark build failure as a blocking bug." in tester_1
    assert "Specialization: Integration & Risk Tester." in tester_4
    assert "Delivery risk acceptability." in tester_4


def test_tester_prompt_requires_runnable_validation_when_available(tmp_path: Path) -> None:
    prompt = PromptBuilder().build(make_context(tmp_path, role="tester", agent_id="tester-1", role_count=1))

    assert "you must run them" in prompt
    assert "do not replace runnable verification with static inspection" in prompt
    assert "set `test_result_code: -1` and `bug_result_code: -1`" in prompt


def test_prompt_builder_has_planner_peer_review_contract(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="planner", agent_id="planner-1", role_count=2)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "PLANNING_PEER_REVIEW",
            "required_outputs": ["peer_review_result.json", "delivery.md"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "planner peer-review phase" in prompt
    assert "Collaboration protocol: PROPOSE -> CRITIQUE -> REVISE -> VOTE -> MERGE" in prompt
    assert "Allowed message intents for this step: critique, ask, block" in prompt
    assert "Set `peer_review_result.json.peer_review_code`" in prompt
    assert "`1` when any plan needs revision" in prompt


def test_prompt_builder_planner_revision_prioritizes_plan_review_feedback(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="planner", agent_id="planner-1", role_count=2)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "PLANNING_REVISION",
            "required_outputs": ["plan.md", "assumptions.md", "risk.md", "todo_breakdown.json", "delivery.md"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "review_result.json" in prompt
    assert "authoritative revision request" in prompt
    assert "do not re-litigate old planner proposals" in prompt


def test_prompt_builder_planner_revision_handles_fix_tester_plan_recheck(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="planner", agent_id="planner-1", role_count=1)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "PLANNING_REVISION",
            "required_outputs": ["plan.md", "assumptions.md", "risk.md", "todo_breakdown.json", "delivery.md"],
            "metadata": {"phase_scope_loop_type": "fix_tester_plan_recheck"},
        }
    )

    prompt = PromptBuilder().build(context)

    assert "fix/test plan recheck" in prompt
    assert "repeated executor fix and tester failure cycles" in prompt
    assert "selected_plan.json" in prompt
    assert "no plan change is needed" in prompt


def test_prompt_builder_has_plan_review_merge_contract(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="reviewer", agent_id="reviewer-1", role_count=1)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "PLAN_REVIEW",
            "required_outputs": ["review_result.json", "selected_plan.json", "delivery.md"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "planning merge-review phase" in prompt
    assert "Current collaboration step: MERGE" in prompt
    assert "Merge the current-round planner" in prompt
    assert "`selected_plan.json` is the single authoritative plan" in prompt
    assert "environment_contract.json" in prompt
    assert "validation_contract.json" in prompt
    assert "authoritative downstream contract set" in prompt
    assert "selected_plan.json.acceptance_oracles" in prompt
    assert "Do not equate plan selection with acceptance selection" in prompt
    assert "any proposal" in prompt
    assert "non-selected, partially selected, or rejected proposal" in prompt
    assert "proposal B" not in prompt
    assert "Do not merely pick one planner proposal" in prompt
    assert "do not create `review_report.md`" in prompt
    assert "review_decision_code` meanings" in prompt
    assert "reviewer_integrated_findings" in prompt
    assert "review_status`, `decision`, or `status`" in prompt
    assert 'acceptance_oracles[*].kind` must be exactly one of `"manual"`, `"runtime"`, `"static"`, or `"test"`' in prompt
    assert "acceptance_oracles[*].verification_mode_code" in prompt
    assert 'acceptance_oracles[*].owner` must be exactly one of `"tester"`, `"reviewer"`, `"external_evaluator"`, `"harness"`, or `"manual"`' in prompt
    assert 'Do not use `"executor"` as an acceptance oracle owner' in prompt
    assert 'acceptance_oracles[*].stage` must be exactly one of `"pre_delivery"`, `"post_delivery"`, `"runtime_readiness"`, `"regression"`, or `"manual"`' in prompt


def test_prompt_builder_includes_retry_feedback(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="reviewer", agent_id="reviewer-1", role_count=1)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "PLAN_REVIEW",
            "retry_feedback": ["selected_plan.json.acceptance_oracles[0].owner must be one of: external_evaluator, harness, manual, reviewer, tester"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "## Previous Attempt Feedback" in prompt
    assert "Harness rejected the previous attempt" in prompt
    assert "selected_plan.json.acceptance_oracles[0].owner" in prompt


def test_tester_prompt_uses_environment_and_validation_contracts(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="tester", agent_id="tester-1", role_count=1)

    prompt = PromptBuilder().build(context)

    assert "environment_contract.json" in prompt
    assert "validation_contract.json" in prompt
    assert "Do not silently fall back to `python -m pytest -q`" in prompt
    assert 'failure_type: "contract_bug"' in prompt


def test_prompt_builder_reviewer_requires_runtime_verdict_json(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="reviewer", agent_id="reviewer-1", role_count=1)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "REVIEWING",
            "required_outputs": ["review_result.json", "delivery.md"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "run the repository on this machine" in prompt
    assert "review_result.json" in prompt
    assert "Treat `tester_result.json` as the structured test verdict" in prompt
    assert "do not request source changes solely because `runtime_readiness.md` ran a generic/default command" in prompt
    assert '"review_decision_code":0' in prompt
    assert "review_status`, `decision`, or `status`" in prompt
    assert "environment_check.status: blocked" in prompt
    assert "routes to tester-owned environment repair" in prompt


def test_prompt_builder_executor_allows_recorded_noop_fix(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="executor", agent_id="executor-1", role_count=1)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "REVIEW_FIXING",
            "required_outputs": ["fix_schedule.md", "fix_patch.diff", "fix_notes.md", "self_check.md", "delivery.md"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "valid no-op fix" in prompt
    assert "no_op_fix: true" in prompt
    assert "empty `fix_patch.diff`" in prompt
    assert "do not recreate or paste a historical patch" in prompt


def test_prompt_builder_injects_communicator_publish_metadata(tmp_path: Path) -> None:
    context = AgentRunContext(
        task_id="task-1",
        phase_id="phase-1",
        phase="DELIVERY",
        role="communicator",
        agent_id="communicator-1",
        round_id=0,
        user_prompt="Build a small weather application.",
        role_instruction="Create final delivery artifacts.",
        workspace_dir=tmp_path / "workspace",
        repo_dir=tmp_path / "workspace" / "repo",
        input_dir=tmp_path / "workspace" / "input",
        output_dir=tmp_path / "workspace" / "output",
        log_dir=tmp_path / "workspace" / "logs",
        input_artifacts=[],
        required_outputs=["final_delivery.json", "usage_guide.md", "delivery.md"],
        timeout_seconds=30,
        config={},
        metadata={
            "expected_success_path": str(tmp_path / "deliver" / "weather-task"),
            "publish_timing": "Harness will publish these files after the communicator role succeeds.",
        },
    )

    prompt = PromptBuilder().build(context)

    assert "## Harness Metadata" in prompt
    assert f"- expected_success_path: {tmp_path / 'deliver' / 'weather-task'}" in prompt
    assert "Use `selected_plan.json`, final executor artifacts, final `bug_report.md`, and final `tester_result.json` as your primary sources of truth" in prompt
    assert "The expected success path is precomputed before publishing" in prompt
    assert "`## Actual Usage` section written for the end user" in prompt
    assert "enter project directory, install dependencies when needed, run the program or tests" in prompt
