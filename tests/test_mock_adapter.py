from __future__ import annotations

from pathlib import Path

from harness.adapters.mock_adapter import MockAgentAdapter
from harness.agents.context import AgentRunContext
from harness.artifacts.schemas import required_outputs_for


def test_mock_adapter_generates_planner_outputs(tmp_path: Path) -> None:
    required = required_outputs_for("planner", "PLANNING_DRAFT")
    context = AgentRunContext(
        task_id="task",
        phase_id="phase",
        phase="PLANNING_DRAFT",
        role="planner",
        agent_id="planner-1",
        round_id=0,
        user_prompt="do work",
        role_instruction="plan",
        workspace_dir=tmp_path,
        repo_dir=tmp_path / "repo",
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        log_dir=tmp_path / "logs",
        required_outputs=required,
    )
    context.repo_dir.mkdir()
    context.input_dir.mkdir()

    result = MockAgentAdapter().run(context)

    assert result.status == "COMPLETED"
    for name in required:
        assert (context.output_dir / name).exists()

