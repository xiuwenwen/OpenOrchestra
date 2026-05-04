from __future__ import annotations

import re
from pathlib import Path

from harness.agents.context import AgentRunContext
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

    assert "`merged_patch.diff` is the authoritative implementation artifact" in prompt
    assert "raw `patch.diff` and `fix_patch.diff` as non-authoritative" in prompt


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
        required_outputs=["merged_patch.diff", "merge_report.md", "delivery.md"],
        timeout_seconds=30,
        config={"roles": {"executor": {"count": 1}}},
    )

    prompt = PromptBuilder().build(context)

    assert "model-driven PATCH_MERGE phase" in prompt
    assert "Do not concatenate blindly" in prompt
    assert "exactly one authoritative `merged_patch.diff`" in prompt
    assert "do not paste a large merged diff as a Write-tool payload" in prompt


def test_prompt_builder_uses_balanced_planner_when_count_is_one(tmp_path: Path) -> None:
    prompt = PromptBuilder().build(make_context(tmp_path, role="planner", agent_id="planner-1", role_count=1))

    assert "Specialization: Balanced Planner." in prompt
    assert "Balanced planning, covering MVP feasibility" in prompt
    assert "Define clear validation criteria for Tester." in prompt


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
    assert "status: changes_requested" in prompt


def test_prompt_builder_has_plan_review_selection_contract(tmp_path: Path) -> None:
    context = make_context(tmp_path, role="reviewer", agent_id="reviewer-1", role_count=1)
    context = AgentRunContext(
        **{
            **context.__dict__,
            "phase": "PLAN_REVIEW",
            "required_outputs": ["review_report.md", "delivery.md"],
        }
    )

    prompt = PromptBuilder().build(context)

    assert "planning review phase" in prompt
    assert "select the best planner proposal by `agent_id`" in prompt
