from __future__ import annotations

from pathlib import Path

from harness.agents.context import AgentRunContext
from harness.agents.delivery_review import (
    DELIVERY_CONTRACT_REVIEW_PROMPT_VERSION,
    DeliveryContractReviewer,
)
from harness.artifacts.validator import ValidationIssue, ValidationResult


def _context(tmp_path: Path) -> AgentRunContext:
    output_dir = tmp_path / "output"
    log_dir = tmp_path / "logs"
    output_dir.mkdir()
    log_dir.mkdir()
    (output_dir / "plan.md").write_text("artifact_result_code: 0\n\n# Plan\n", encoding="utf-8")
    (output_dir / "delivery.md").write_text("status: success\nsummary: complete\n", encoding="utf-8")
    (log_dir / "prompt.md").write_text("# Failed Agent Prompt\nProduce plan.md and delivery.md.", encoding="utf-8")
    return AgentRunContext(
        task_id="task-1",
        phase_id="phase-1",
        phase="PLANNING_DRAFT",
        role="planner",
        agent_id="planner-1",
        round_id=0,
        user_prompt="Plan a feature.",
        role_instruction="Plan only.",
        workspace_dir=tmp_path,
        repo_dir=tmp_path / "repo",
        input_dir=tmp_path / "input",
        output_dir=output_dir,
        log_dir=log_dir,
        input_artifacts=[],
        required_outputs=["plan.md", "delivery.md"],
        timeout_seconds=0,
        config={},
    )


def test_delivery_contract_review_prompt_embeds_failed_prompt_and_delivery(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = ValidationResult(
        (
            ValidationIssue("delivery.md", "missing_return_code", "delivery.md must contain `return_code: <int>`"),
        )
    )

    prompt = DeliveryContractReviewer().build_prompt(context, result)

    assert DELIVERY_CONTRACT_REVIEW_PROMPT_VERSION in prompt
    assert "Return exactly one JSON object and no Markdown/prose/code fence." in prompt
    assert '"decision": "accept|retry"' in prompt
    assert "## Failed Agent Task Prompt" in prompt
    assert "Produce plan.md and delivery.md." in prompt
    assert "## Submitted delivery.md" in prompt
    assert "status: success" in prompt


def test_delivery_contract_review_parses_json_decision(tmp_path: Path) -> None:
    review = DeliveryContractReviewer()._parse_review(
        '{"decision":"accept","delivery_return_code":0,"instruction_following_issue":true,'
        '"actual_role_success":true,"reason":"format only"}',
        tmp_path / "prompt.md",
        tmp_path / "stdout.log",
        tmp_path / "stderr.log",
    )

    assert review.accepts
    assert review.delivery_return_code == 0
    assert review.reason == "format only"
