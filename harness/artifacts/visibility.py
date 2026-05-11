from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.artifacts.schemas import (
    ANY_PHASE,
    ARTIFACT_VISIBILITY_RULES,
    CONDITION_HAS_REJECTED_PLAN_REVIEW,
    CONDITION_NO_REJECTED_PLAN_REVIEW,
    JUDGE_DECISION_ARTIFACTS,
    ROUND_ANY,
    ROUND_BEFORE_CURRENT,
    ROUND_CURRENT,
    ROUND_LATEST_BEFORE_CURRENT_PER_TYPE,
    ROUND_LATEST_COMPLETE_JUDGE_BEFORE_CURRENT,
    ROUND_LATEST_COMPLETE_TEST_BEFORE_CURRENT,
    ROUND_LATEST_PER_TYPE,
    ROUND_LATEST_PLANNING,
    ROUND_PREVIOUS,
    ROUND_REJECTED_PLAN_REVIEW,
    SELF_EXCLUDE,
    TEST_REPORT_ARTIFACTS,
    ArtifactVisibilityRule,
    _phases,
    role_phase_contract_for,
)
from harness.core.state_machine import (
    PLAN_REVIEW,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_JUDGEMENT,
    TEST_JUDGEMENT,
    TESTING,
)


class ArtifactVisibilityPolicy:
    def filter_visible_artifacts(
        self,
        artifacts: list[dict[str, Any]],
        phases_by_id: dict[str, dict[str, Any]],
        role: str,
        phase: str,
        round_id: int | None,
        current_agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        filtered_ids: set[str] = set()
        latest_matches: dict[tuple[int, str], dict[str, Any]] = {}
        rules = self.rules_for(role, phase)
        latest_planning_round = max(
            (
                int(phases_by_id[artifact["phase_id"]]["round_id"])
                for artifact in artifacts
                if artifact.get("phase_id") in phases_by_id
                and (artifact.get("role") or "") == "planner"
                and phases_by_id[artifact["phase_id"]]["phase_type"]
                in {PLANNING_DRAFT, PLANNING_REVISION, PLANNING_PEER_REVIEW}
                and phases_by_id[artifact["phase_id"]].get("round_id") is not None
            ),
            default=None,
        )
        rejected_plan_review_round = self._latest_rejected_plan_review_round(artifacts, phases_by_id, round_id)
        latest_complete_test_round = self._latest_complete_artifact_round_before(
            artifacts,
            phases_by_id,
            round_id,
            source_role="tester",
            artifact_types=TEST_REPORT_ARTIFACTS,
            source_phases=_phases(TESTING, REGRESSION_TESTING),
        )
        latest_complete_judge_round = self._latest_complete_artifact_round_before(
            artifacts,
            phases_by_id,
            round_id,
            source_role="judge",
            artifact_types=JUDGE_DECISION_ARTIFACTS,
            source_phases=_phases(TEST_JUDGEMENT, REVIEW_JUDGEMENT),
        )

        def append_once(artifact: dict[str, Any]) -> None:
            artifact_id = str(artifact.get("artifact_id") or artifact.get("path") or id(artifact))
            if artifact_id in filtered_ids:
                return
            filtered_ids.add(artifact_id)
            filtered.append(artifact)

        for artifact in artifacts:
            artifact_type = artifact["artifact_type"]
            phase_row = phases_by_id.get(artifact.get("phase_id") or "")
            artifact_round = int(phase_row["round_id"]) if phase_row and phase_row.get("round_id") is not None else None
            artifact_phase = str(phase_row["phase_type"]) if phase_row else None
            effective_round = artifact_round if artifact_round is not None else self._artifact_declared_round_id(artifact)
            for rule_index, rule in enumerate(rules):
                if not self._artifact_matches_visibility_rule(
                    artifact,
                    rule,
                    artifact_phase=artifact_phase,
                    effective_round=effective_round,
                    target_round=round_id,
                    current_agent_id=current_agent_id,
                    latest_planning_round=latest_planning_round,
                    rejected_plan_review_round=rejected_plan_review_round,
                    latest_complete_test_round=latest_complete_test_round,
                    latest_complete_judge_round=latest_complete_judge_round,
                ):
                    continue
                if rule.round_policy in {ROUND_LATEST_PER_TYPE, ROUND_LATEST_BEFORE_CURRENT_PER_TYPE}:
                    key = (rule_index, artifact_type)
                    current = latest_matches.get(key)
                    if current is None or self._artifact_order_key(
                        artifact,
                        phases_by_id,
                    ) > self._artifact_order_key(current, phases_by_id):
                        latest_matches[key] = artifact
                else:
                    append_once(artifact)
                break

        for artifact in latest_matches.values():
            append_once(artifact)
        return filtered

    def rules_for(self, role: str, phase: str) -> list[ArtifactVisibilityRule]:
        contract = role_phase_contract_for(role, phase)
        if contract.visibility_rules:
            return list(contract.visibility_rules)
        return [rule for rule in ARTIFACT_VISIBILITY_RULES if rule.target_role == role and rule.target_phase in {phase, ANY_PHASE}]

    def allowed_by_table(self, target_role: str, target_phase: str, source_role: str, artifact_type: str) -> bool:
        for rule in self.rules_for(target_role, target_phase):
            if rule.source_role == source_role and artifact_type in rule.artifact_types:
                return True
        return False

    def _artifact_order_key(
        self,
        artifact: dict[str, Any],
        phases_by_id: dict[str, dict[str, Any]],
    ) -> tuple[int, int, str, str]:
        phase_row = phases_by_id.get(artifact.get("phase_id") or "")
        round_value = -1
        if phase_row and phase_row.get("round_id") is not None:
            try:
                round_value = int(phase_row["round_id"])
            except (TypeError, ValueError):
                round_value = -1
        else:
            declared_round = self._artifact_declared_round_id(artifact)
            if declared_round is not None:
                round_value = declared_round
        try:
            version = int(artifact.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        return (
            round_value,
            version,
            str(artifact.get("created_at") or ""),
            str(artifact.get("artifact_id") or artifact.get("path") or ""),
        )

    def _artifact_matches_visibility_rule(
        self,
        artifact: dict[str, Any],
        rule: ArtifactVisibilityRule,
        *,
        artifact_phase: str | None,
        effective_round: int | None,
        target_round: int | None,
        current_agent_id: str | None,
        latest_planning_round: int | None,
        rejected_plan_review_round: int | None,
        latest_complete_test_round: int | None,
        latest_complete_judge_round: int | None,
    ) -> bool:
        artifact_type = artifact["artifact_type"]
        artifact_role = artifact.get("role")
        if rule.source_role == "context" and artifact_type == "project_context.md":
            if artifact_role not in {None, "", "context"}:
                return False
        elif (artifact_role or "") != rule.source_role:
            return False
        if artifact_type not in rule.artifact_types:
            return False
        if rule.source_phases is not None and artifact_phase not in rule.source_phases:
            return False
        if rule.self_policy == SELF_EXCLUDE and current_agent_id and artifact.get("agent_id") == current_agent_id:
            return False
        if rule.condition == CONDITION_HAS_REJECTED_PLAN_REVIEW and rejected_plan_review_round is None:
            return False
        if rule.condition == CONDITION_NO_REJECTED_PLAN_REVIEW and rejected_plan_review_round is not None:
            return False
        return self._artifact_round_matches_visibility_rule(
            rule,
            effective_round=effective_round,
            target_round=target_round,
            latest_planning_round=latest_planning_round,
            rejected_plan_review_round=rejected_plan_review_round,
            latest_complete_test_round=latest_complete_test_round,
            latest_complete_judge_round=latest_complete_judge_round,
        )

    def _artifact_round_matches_visibility_rule(
        self,
        rule: ArtifactVisibilityRule,
        *,
        effective_round: int | None,
        target_round: int | None,
        latest_planning_round: int | None,
        rejected_plan_review_round: int | None,
        latest_complete_test_round: int | None,
        latest_complete_judge_round: int | None,
    ) -> bool:
        if rule.round_policy == ROUND_ANY:
            return True
        if rule.round_policy == ROUND_LATEST_PER_TYPE:
            return True
        if rule.round_policy == ROUND_LATEST_PLANNING:
            return latest_planning_round is not None and effective_round == latest_planning_round
        if rule.round_policy == ROUND_REJECTED_PLAN_REVIEW:
            return rejected_plan_review_round is not None and effective_round == rejected_plan_review_round
        if rule.round_policy == ROUND_LATEST_COMPLETE_TEST_BEFORE_CURRENT:
            return latest_complete_test_round is not None and effective_round == latest_complete_test_round
        if rule.round_policy == ROUND_LATEST_COMPLETE_JUDGE_BEFORE_CURRENT:
            return latest_complete_judge_round is not None and effective_round == latest_complete_judge_round
        if rule.round_policy == ROUND_BEFORE_CURRENT and target_round is None:
            return True
        if target_round is None:
            return False
        if effective_round is None:
            return False
        if rule.round_policy == ROUND_CURRENT:
            return effective_round == target_round
        if rule.round_policy == ROUND_PREVIOUS:
            return effective_round == max(0, target_round - 1)
        if rule.round_policy == ROUND_BEFORE_CURRENT:
            return effective_round < target_round
        if rule.round_policy == ROUND_LATEST_BEFORE_CURRENT_PER_TYPE:
            return effective_round < target_round
        raise ValueError(f"Unknown artifact visibility round_policy: {rule.round_policy}")

    def _latest_complete_artifact_round_before(
        self,
        artifacts: list[dict[str, Any]],
        phases_by_id: dict[str, dict[str, Any]],
        target_round: int | None,
        *,
        source_role: str,
        artifact_types: frozenset[str],
        source_phases: frozenset[str],
    ) -> int | None:
        if target_round is None:
            return None
        artifact_types_by_round: dict[int, set[str]] = {}
        for artifact in artifacts:
            if (artifact.get("role") or "") != source_role:
                continue
            artifact_type = str(artifact.get("artifact_type") or "")
            if artifact_type not in artifact_types:
                continue
            phase_row = phases_by_id.get(artifact.get("phase_id") or "")
            if not phase_row or phase_row.get("phase_type") not in source_phases or phase_row.get("round_id") is None:
                continue
            artifact_round = int(phase_row["round_id"])
            if artifact_round >= target_round:
                continue
            artifact_types_by_round.setdefault(artifact_round, set()).add(artifact_type)
        complete_rounds = [
            artifact_round
            for artifact_round, round_artifact_types in artifact_types_by_round.items()
            if artifact_types <= round_artifact_types
        ]
        return max(complete_rounds, default=None)

    def _latest_rejected_plan_review_round(
        self,
        artifacts: list[dict[str, Any]],
        phases_by_id: dict[str, dict[str, Any]],
        current_round_id: int | None,
    ) -> int | None:
        latest_round: int | None = None
        for artifact in artifacts:
            if (artifact.get("role") or "") != "reviewer" or artifact.get("artifact_type") != "review_report.md":
                continue
            phase_row = phases_by_id.get(artifact.get("phase_id") or "")
            if not phase_row or phase_row.get("phase_type") != PLAN_REVIEW or phase_row.get("round_id") is None:
                continue
            artifact_round = int(phase_row["round_id"])
            if current_round_id is not None and artifact_round >= current_round_id:
                continue
            path = Path(str(artifact.get("path") or ""))
            if not path.exists() or not self._review_report_requests_changes(path):
                continue
            if latest_round is None or artifact_round > latest_round:
                latest_round = artifact_round
        return latest_round

    def _review_report_requests_changes(self, path: Path) -> bool:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        return bool(re.search(r"(?m)^\s*review_decision_code\s*:\s*(1|-1)\s*$", text))

    def _artifact_declared_round_id(self, artifact: dict[str, Any]) -> int | None:
        path = Path(str(artifact.get("path") or ""))
        if not path.exists() or not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        value = self._markdown_field(content, "round_id")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _markdown_field(self, content: str, field_name: str) -> str | None:
        prefix = f"{field_name}:"
        for line in content.splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
        return None
