from __future__ import annotations

from typing import Any


class MockJudge:
    def normalize(self, phase: str, raw_payload: dict[str, Any]) -> dict[str, Any]:
        return {"phase": phase, **raw_payload}

    def is_plan_approved(self, payload: dict[str, Any]) -> bool:
        decision = self._decision_value(payload)
        return decision in {"approve", "approved", "pass", "passed", "proceed", "proceed_with_caution"} and not payload.get("changes_required", False)

    def is_test_pass(self, payload: dict[str, Any]) -> bool:
        if payload.get("tests_passed") is False:
            return False
        decision = self._decision_value(payload)
        return decision in {"pass", "passed", "approve", "approved", "proceed", "proceed_with_caution"} or payload.get("tests_passed") is True

    def is_review_approved(self, payload: dict[str, Any]) -> bool:
        decision = self._decision_value(payload)
        return decision in {"approve", "approved", "pass", "passed", "proceed", "proceed_with_caution"} and not payload.get("changes_required", False)

    def is_final_approved(self, payload: dict[str, Any]) -> bool:
        decision = self._decision_value(payload)
        return decision in {"approve", "approved", "pass", "passed", "proceed", "proceed_with_caution"} or payload.get("final_approved") is True

    def _decision_value(self, payload: dict[str, Any]) -> str:
        decision = payload.get("decision") or payload.get("status") or payload.get("result")
        if isinstance(decision, dict):
            decision = decision.get("action") or decision.get("status") or decision.get("decision")
        if decision is None:
            return ""
        return str(decision).strip().lower()
