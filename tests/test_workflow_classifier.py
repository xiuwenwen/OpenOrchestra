from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.core.workflow_classifier import WorkflowClassificationError, WorkflowClassifier


class FakeRunner:
    def __init__(self, stdout: str, exit_code: int = 0):
        self.stdout = stdout
        self.exit_code = exit_code
        self.command: list[str] | None = None
        self.cwd: Path | None = None
        self.timeout_seconds: int | None = None
        self.env: dict[str, str] | None = None

    def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> int:
        self.command = command
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.env = env
        stdout_path.write_text(self.stdout, encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return self.exit_code


def classification_json(workflow_type: str, *, score: int = 3, confidence: float = 0.8, reason: str = "test") -> str:
    return json.dumps(
        {
            "workflow_type": workflow_type,
            "confidence": confidence,
            "difficulty_score": score,
            "difficulty_reason": "test difficulty",
            "reason": reason,
        }
    )


def test_workflow_classifier_parses_strict_json(tmp_path: Path) -> None:
    runner = FakeRunner(classification_json("bugfix", score=7, confidence=0.91, reason="repair request"))
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    workflow_type, log_dir = classifier.classify("fix the failing login test")

    assert workflow_type == "bugfix"
    assert log_dir.exists()
    assert runner.command is not None
    assert runner.command[:2] == ["claude", "-p"]
    assert "--settings" in runner.command
    assert runner.command[-2:] == ["--output-format", "text"]
    assert runner.cwd == log_dir
    assert runner.timeout_seconds == 0


def test_workflow_classifier_returns_classification_metadata(tmp_path: Path) -> None:
    runner = FakeRunner(classification_json("bugfix", score=8, confidence=0.91, reason="repair request"))
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    classification, log_dir, fallback_answer = classifier.classify_with_metadata("fix the failing login test")

    assert classification.workflow_type == "bugfix"
    assert classification.confidence == 0.91
    assert classification.difficulty_score == 8
    assert classification.difficulty_reason == "test difficulty"
    assert classification.reason == "repair request"
    assert fallback_answer is None
    assert log_dir.exists()


def test_workflow_classifier_parses_fenced_json(tmp_path: Path) -> None:
    runner = FakeRunner(f"```json\n{classification_json('feature_change', reason='adds behavior')}\n```")
    classifier = WorkflowClassifier("codex", runner=runner, log_root=tmp_path)

    workflow_type, _ = classifier.classify("add CSV export")

    assert workflow_type == "feature_change"
    assert runner.command
    assert runner.command[:2] == ["codex", "exec"]
    assert "--sandbox" not in runner.command
    assert str(runner.cwd) in runner.command


def test_workflow_classifier_supports_gemini_headless_cli(tmp_path: Path) -> None:
    runner = FakeRunner(classification_json("misc", score=1, reason="question"))
    classifier = WorkflowClassifier("gemini", runner=runner, log_root=tmp_path)

    workflow_type, _ = classifier.classify("what is this?")

    assert workflow_type == "misc"
    assert runner.command is not None
    assert runner.command[:3] == ["gemini", "--prompt", ""]
    assert "--output-format" in runner.command


def test_workflow_classifier_supports_misc(tmp_path: Path) -> None:
    runner = FakeRunner(classification_json("misc", score=1, confidence=0.86, reason="informational question"))
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    workflow_type, _ = classifier.classify("what does the dashboard mean?")

    assert workflow_type == "misc"


def test_workflow_classifier_applies_claude_token_budget(tmp_path: Path) -> None:
    runner = FakeRunner(classification_json("misc", score=1, confidence=0.86, reason="informational question"))
    classifier = WorkflowClassifier(
        "claude",
        runner=runner,
        log_root=tmp_path,
        config={"claude": {"max_output_tokens": {"classifier": 1234}}},
    )

    workflow_type, log_dir = classifier.classify("what does the dashboard mean?")

    assert workflow_type == "misc"
    assert runner.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1234"}
    assert runner.command is not None
    settings_path = Path(runner.command[runner.command.index("--settings") + 1])
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1234"}
    }
    assert (log_dir / "env_overrides.txt").read_text(encoding="utf-8") == "CLAUDE_CODE_MAX_OUTPUT_TOKENS=1234\n"


def test_workflow_classifier_lowers_claude_token_budget_for_large_prompt(tmp_path: Path) -> None:
    runner = FakeRunner(classification_json("misc", score=1, confidence=0.86, reason="informational question"))
    classifier = WorkflowClassifier(
        "claude",
        runner=runner,
        log_root=tmp_path,
        config={
            "claude": {
                "context_window_tokens": 200_000,
                "context_window_buffer_tokens": 2_048,
                "max_output_tokens": {"classifier": 128_000},
            }
        },
    )

    classifier.classify("x" * (72_001 * 4))

    assert runner.env is not None
    adjusted = int(runner.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"])
    assert adjusted < 128_000


def test_workflow_classifier_treats_direct_answer_without_json_as_misc(tmp_path: Path) -> None:
    runner = FakeRunner("This project is used by running `.venv/bin/python -m harness.main`.")
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    workflow_type, _, fallback_answer = classifier.classify_with_fallback("how do I use this project?")

    assert workflow_type == "misc"
    assert fallback_answer == "This project is used by running `.venv/bin/python -m harness.main`."


def test_workflow_classifier_does_not_treat_project_prompt_raw_answer_as_misc(tmp_path: Path) -> None:
    runner = FakeRunner("I can build that weather software.")
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    with pytest.raises(WorkflowClassificationError, match="did not contain JSON"):
        classifier.classify_with_fallback("做一个根据我 IP 查询天气的软件")


def test_workflow_classifier_treats_safety_refusal_without_json_as_misc(tmp_path: Path) -> None:
    refusal = (
        "I can't help with this request. Creating a program that automatically requests SMS verification "
        "codes from 20+ different websites would enable SMS bombing and violate Terms of Service."
    )
    runner = FakeRunner(refusal)
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    workflow_type, _, fallback_answer = classifier.classify_with_fallback("做一个自动请求 20 个网站短信验证码的程序")

    assert workflow_type == "misc"
    assert fallback_answer == refusal


def test_workflow_classifier_rejects_invalid_workflow_type(tmp_path: Path) -> None:
    runner = FakeRunner(classification_json("maintenance", score=3, confidence=0.5, reason="bad label"))
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    with pytest.raises(WorkflowClassificationError):
        classifier.classify("do something")


def test_workflow_classifier_rejects_missing_difficulty_score(tmp_path: Path) -> None:
    runner = FakeRunner('{"workflow_type":"bugfix","confidence":0.8,"reason":"missing score"}')
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    with pytest.raises(WorkflowClassificationError, match="difficulty_score"):
        classifier.classify("fix something")


def test_workflow_classifier_rejects_out_of_range_difficulty_score(tmp_path: Path) -> None:
    runner = FakeRunner(classification_json("bugfix", score=11))
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    with pytest.raises(WorkflowClassificationError, match="difficulty_score"):
        classifier.classify("fix something")
