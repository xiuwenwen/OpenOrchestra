from __future__ import annotations

from harness.judge.judge_runner import MockJudge


def test_judge_rejects_test_pass_when_tests_passed_is_false() -> None:
    judge = MockJudge()

    assert not judge.is_test_pass({"decision": "pass", "tests_passed": False})


def test_judge_plan_approval_respects_changes_required() -> None:
    judge = MockJudge()

    assert judge.is_plan_approved({"decision": "approved", "changes_required": False})
    assert not judge.is_plan_approved({"decision": "approved", "changes_required": True})
