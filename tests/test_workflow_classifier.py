from __future__ import annotations

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


def test_workflow_classifier_parses_strict_json(tmp_path: Path) -> None:
    runner = FakeRunner('{"workflow_type":"bugfix","confidence":0.91,"reason":"repair request"}')
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    workflow_type, log_dir = classifier.classify("fix the failing login test")

    assert workflow_type == "bugfix"
    assert log_dir.exists()
    assert runner.command == ["claude", "-p", "--output-format", "text"]
    assert runner.cwd == log_dir
    assert runner.timeout_seconds == 0


def test_workflow_classifier_parses_fenced_json(tmp_path: Path) -> None:
    runner = FakeRunner('```json\n{"workflow_type":"feature_change","confidence":0.8,"reason":"adds behavior"}\n```')
    classifier = WorkflowClassifier("codex", runner=runner, log_root=tmp_path)

    workflow_type, _ = classifier.classify("add CSV export")

    assert workflow_type == "feature_change"
    assert runner.command
    assert runner.command[:2] == ["codex", "exec"]
    assert "--sandbox" not in runner.command
    assert str(runner.cwd) in runner.command


def test_workflow_classifier_supports_misc(tmp_path: Path) -> None:
    runner = FakeRunner('{"workflow_type":"misc","confidence":0.86,"reason":"informational question"}')
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    workflow_type, _ = classifier.classify("what does the dashboard mean?")

    assert workflow_type == "misc"


def test_workflow_classifier_applies_claude_token_budget(tmp_path: Path) -> None:
    runner = FakeRunner('{"workflow_type":"misc","confidence":0.86,"reason":"informational question"}')
    classifier = WorkflowClassifier(
        "claude",
        runner=runner,
        log_root=tmp_path,
        config={"claude": {"max_output_tokens": {"classifier": 1234}}},
    )

    workflow_type, log_dir = classifier.classify("what does the dashboard mean?")

    assert workflow_type == "misc"
    assert runner.env == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1234"}
    assert (log_dir / "env_overrides.txt").read_text(encoding="utf-8") == "CLAUDE_CODE_MAX_OUTPUT_TOKENS=1234\n"


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


def test_workflow_classifier_rejects_invalid_workflow_type(tmp_path: Path) -> None:
    runner = FakeRunner('{"workflow_type":"maintenance","confidence":0.5,"reason":"bad label"}')
    classifier = WorkflowClassifier("claude", runner=runner, log_root=tmp_path)

    with pytest.raises(WorkflowClassificationError):
        classifier.classify("do something")
