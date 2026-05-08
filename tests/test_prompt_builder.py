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
    assert "## Output Contract" in prompt
    assert "## Prohibited Actions" in prompt
    assert "valid unified diff" in prompt
    assert "git add -N . && git diff --no-ext-diff" in prompt
    assert "Do not paste a large patch into a Write-tool payload" in prompt
    assert not re.search(r"[\u4e00-\u9fff]", prompt)


def test_prompt_builder_marks_merged_patch_as_authoritative(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="tester", agent_id="tester-1", role_count=1)

    prompt = PromptBuilder().build(context)

    assert "Treat the repository directory as the implementation under test" in prompt
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
    assert "must each contain `artifact_result_code: 0` somewhere in the file" in prompt
    assert "line 1 must be `artifact_result_code: 0`" not in prompt
    assert "put headings such as `# Report` only after that line" not in prompt
    assert "Do not copy `build_result_code`, `test_result_code`, or `bug_result_code` values" in prompt
    assert "build_result_code: -1" in prompt
    assert "test_result_code: 2" in prompt
    assert "bug_result_code: -1" in prompt
    assert "`artifact_result_code: -1`" not in prompt
    assert "`artifact_result_code: 2`" not in prompt


def test_prompt_builder_has_model_driven_patch_merge_contract(tmp_path: Path) -> None:
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

    assert "model-driven PATCH_MERGE phase" in prompt
    assert "Do not concatenate blindly" in prompt
    assert "exactly one authoritative `merged_patch.diff`" in prompt
    assert "do not paste a large merged diff as a Write-tool payload" in prompt
    assert "`patch_metadata.md`" in prompt
    assert "`merged_patch_metadata.md`" in prompt
    assert "Do not select a patch based only on filename" in prompt
    assert "Prior `merged_patch.diff` artifacts are historical evidence" in prompt


def test_executor_patch_outputs_require_baseline_metadata(tmp_path: Path) -> None:
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

    assert "Produce `patch_metadata.md` next to the patch" in prompt
    assert "`base_source_path`" in prompt
    assert "For FIXING and REVIEW_FIXING, prefer `patch_scope: incremental_fix`" in prompt
    assert "historical empty project baseline" in prompt


def test_prompt_builder_uses_balanced_planner_when_count_is_one(tmp_path: Path) -> None:
    prompt = PromptBuilder().build(make_context(tmp_path, role="planner", agent_id="planner-1", role_count=1))

    assert "Specialization: Balanced Planner." in prompt
    assert "Balanced planning, covering MVP feasibility" in prompt
    assert "Define clear validation criteria for Tester." in prompt
    assert "`todo_breakdown.md` must use this exact repeated task schema" in prompt
    assert "`files: <target paths or path globs>`" in prompt
    assert "`acceptance_criteria:` with concrete observable outcomes" in prompt
    assert "`test_commands:` with exact commands or `not_applicable: <reason>`" in prompt


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


def test_prompt_builder_has_planner_peer_review_contract(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="planner", agent_id="planner-1", role_count=2)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "PLANNING_PEER_REVIEW",
            "required_outputs": ["peer_review.md", "delivery.md"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "planner peer-review phase" in prompt
    assert "`peer_review.md` must include one machine-readable line" in prompt
    assert "peer_review_code: 1" in prompt


def test_prompt_builder_planner_revision_prioritizes_plan_review_feedback(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="planner", agent_id="planner-1", role_count=2)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "PLANNING_REVISION",
            "required_outputs": ["plan.md", "assumptions.md", "risk.md", "todo_breakdown.md", "delivery.md"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "review_report.md" in prompt
    assert "authoritative revision request" in prompt
    assert "do not re-litigate old planner proposals" in prompt


def test_prompt_builder_hides_generic_error_code_tables_from_judge(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="judge", agent_id="judge-1", role_count=1)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "TEST_JUDGEMENT",
            "required_outputs": ["decision.json", "decision_summary.md", "delivery.md"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "`delivery.md` is the role return envelope, not the task/business verdict." in prompt
    assert "`delivery.md` must contain `return_code: 0`" in prompt
    assert "Return code meanings:" not in prompt
    assert "Markdown artifact result code meanings:" not in prompt
    assert "`decision_summary.md` must contain `artifact_result_code: 0` somewhere in the file" in prompt
    assert "Put the phase verdict only in `decision.json.decision`" in prompt
    assert "Do not copy `decision_code` or `decision.json.decision` into `artifact_result_code` or `return_code`" in prompt
    assert "If you choose `decision: fail` because tests failed, write `return_code: 0` in `delivery.md`" in prompt


def test_prompt_builder_has_plan_review_merge_contract(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="reviewer", agent_id="reviewer-1", role_count=1)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "PLAN_REVIEW",
            "required_outputs": ["review_report.md", "selected_plan.md", "delivery.md"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "planning merge-review phase" in prompt
    assert "Merge the current-round planner" in prompt
    assert "`selected_plan.md` is the single authoritative plan" in prompt
    assert "Do not merely pick one planner proposal" in prompt


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
        required_outputs=["final_delivery.md", "usage_guide.md", "delivery.md"],
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
    assert "The expected success path is precomputed before publishing" in prompt
    assert "`## Actual Usage` section written for the end user" in prompt
    assert "enter project directory, install dependencies when needed, run the program or tests" in prompt
