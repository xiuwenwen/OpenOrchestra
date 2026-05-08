from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.core.state_machine import (
    DELIVERY,
    EXECUTION,
    FINAL_JUDGEMENT,
    FIXING,
    PATCH_MERGE,
    PLAN_REVIEW,
    PLAN_JUDGEMENT,
    PLANNING_DRAFT,
    PLANNING_PEER_REVIEW,
    PLANNING_REVISION,
    REGRESSION_TESTING,
    REVIEW_FIXING,
    REVIEW_JUDGEMENT,
    REVIEWING,
    TEST_JUDGEMENT,
    TESTING,
)


ANY_PHASE = "*"
ROUND_ANY = "any"
ROUND_CURRENT = "current"
ROUND_PREVIOUS = "previous"
ROUND_BEFORE_CURRENT = "before_current"
ROUND_LATEST_PER_TYPE = "latest_per_type"
ROUND_LATEST_BEFORE_CURRENT_PER_TYPE = "latest_before_current_per_type"
ROUND_LATEST_COMPLETE_TEST_BEFORE_CURRENT = "latest_complete_test_before_current"
ROUND_LATEST_COMPLETE_JUDGE_BEFORE_CURRENT = "latest_complete_judge_before_current"
ROUND_LATEST_PLANNING = "latest_planning_round"
ROUND_REJECTED_PLAN_REVIEW = "latest_rejected_plan_review"
SELF_INCLUDE = "include"
SELF_EXCLUDE = "exclude"
CONDITION_ALWAYS = "always"
CONDITION_NO_REJECTED_PLAN_REVIEW = "no_rejected_plan_review"
CONDITION_HAS_REJECTED_PLAN_REVIEW = "has_rejected_plan_review"


@dataclass(frozen=True)
class ArtifactVisibilityRule:
    target_role: str
    target_phase: str
    source_role: str
    artifact_types: frozenset[str]
    source_phases: frozenset[str] | None = None
    round_policy: str = ROUND_ANY
    self_policy: str = SELF_INCLUDE
    condition: str = CONDITION_ALWAYS


def _types(*artifact_types: str) -> frozenset[str]:
    return frozenset(artifact_types)


def _phases(*phases: str) -> frozenset[str]:
    return frozenset(phases)


PLANNING_ARTIFACTS = _types("plan.md", "assumptions.md", "risk.md", "todo_breakdown.md")
TEST_REPORT_ARTIFACTS = _types("build_report.md", "test_report.md", "bug_report.md")
JUDGE_DECISION_ARTIFACTS = _types("decision.json", "decision_summary.md")
PATCH_GATE_ARTIFACTS = _types("test_gate.md", "objective_gate.md", "patch_validation.md", "materialized_repo.md")
TEST_JUDGE_GATE_ARTIFACTS = _types("test_gate.md", "objective_gate.md")
EXECUTOR_REVIEW_ARTIFACTS = _types(
    "implementation_plan.md",
    "changed_files.md",
    "merged_patch.diff",
    "merged_patch_metadata.md",
    "patch_metadata.md",
    "fix_schedule.md",
    "fix_notes.md",
    "self_check.md",
    "merge_report.md",
)
FINAL_EXECUTOR_ARTIFACTS = _types(
    "implementation_plan.md",
    "changed_files.md",
    "merged_patch.diff",
    "merged_patch_metadata.md",
    "patch_metadata.md",
    "fix_schedule.md",
    "fix_notes.md",
    "self_check.md",
    "merge_report.md",
)
FINAL_PLANNER_ARTIFACTS = _types("plan.md", "assumptions.md", "risk.md", "todo_breakdown.md", "peer_review.md")
FINAL_REVIEWER_ARTIFACTS = _types("selected_plan.md", "review_report.md")


ARTIFACT_VISIBILITY_RULES: tuple[ArtifactVisibilityRule, ...] = (
    ArtifactVisibilityRule("planner", PLANNING_DRAFT, "planner", PLANNING_ARTIFACTS, round_policy=ROUND_BEFORE_CURRENT),
    ArtifactVisibilityRule("planner", PLANNING_DRAFT, "judge", JUDGE_DECISION_ARTIFACTS, round_policy=ROUND_BEFORE_CURRENT),
    ArtifactVisibilityRule(
        "planner",
        PLANNING_PEER_REVIEW,
        "planner",
        PLANNING_ARTIFACTS,
        source_phases=_phases(PLANNING_DRAFT, PLANNING_REVISION),
        round_policy=ROUND_CURRENT,
        self_policy=SELF_EXCLUDE,
    ),
    ArtifactVisibilityRule(
        "planner",
        PLANNING_REVISION,
        "planner",
        PLANNING_ARTIFACTS | _types("peer_review.md"),
        round_policy=ROUND_BEFORE_CURRENT,
        condition=CONDITION_NO_REJECTED_PLAN_REVIEW,
    ),
    ArtifactVisibilityRule(
        "planner",
        PLANNING_REVISION,
        "judge",
        JUDGE_DECISION_ARTIFACTS,
        round_policy=ROUND_BEFORE_CURRENT,
        condition=CONDITION_NO_REJECTED_PLAN_REVIEW,
    ),
    ArtifactVisibilityRule(
        "planner",
        PLANNING_REVISION,
        "reviewer",
        _types("review_report.md"),
        source_phases=_phases(PLAN_REVIEW),
        round_policy=ROUND_REJECTED_PLAN_REVIEW,
        condition=CONDITION_HAS_REJECTED_PLAN_REVIEW,
    ),
    ArtifactVisibilityRule(
        "reviewer",
        PLAN_REVIEW,
        "planner",
        PLANNING_ARTIFACTS | _types("peer_review.md"),
        source_phases=_phases(PLANNING_DRAFT, PLANNING_REVISION, PLANNING_PEER_REVIEW),
        round_policy=ROUND_CURRENT,
    ),
    ArtifactVisibilityRule(
        "judge",
        PLAN_JUDGEMENT,
        "planner",
        PLANNING_ARTIFACTS | _types("peer_review.md"),
        source_phases=_phases(PLANNING_DRAFT, PLANNING_REVISION, PLANNING_PEER_REVIEW),
        round_policy=ROUND_CURRENT,
    ),
    ArtifactVisibilityRule(
        "judge",
        PLAN_JUDGEMENT,
        "reviewer",
        _types("selected_plan.md", "review_report.md"),
        source_phases=_phases(PLAN_REVIEW),
        round_policy=ROUND_CURRENT,
    ),
    ArtifactVisibilityRule(
        "executor",
        EXECUTION,
        "reviewer",
        _types("selected_plan.md", "review_report.md"),
        source_phases=_phases(PLAN_REVIEW),
        round_policy=ROUND_LATEST_PLANNING,
    ),
    ArtifactVisibilityRule(
        "executor",
        PATCH_MERGE,
        "executor",
        _types("patch.diff", "fix_patch.diff", "patch_metadata.md"),
        source_phases=_phases(EXECUTION, FIXING, REVIEW_FIXING),
        round_policy=ROUND_CURRENT,
    ),
    ArtifactVisibilityRule(
        "executor",
        PATCH_MERGE,
        "executor",
        _types("merged_patch.diff", "merged_patch_metadata.md"),
        source_phases=_phases(PATCH_MERGE),
        round_policy=ROUND_LATEST_BEFORE_CURRENT_PER_TYPE,
    ),
    ArtifactVisibilityRule("executor", FIXING, "orchestrator", PATCH_GATE_ARTIFACTS, round_policy=ROUND_PREVIOUS),
    ArtifactVisibilityRule(
        "executor",
        FIXING,
        "executor",
        _types("merged_patch_metadata.md"),
        source_phases=_phases(PATCH_MERGE),
        round_policy=ROUND_PREVIOUS,
    ),
    ArtifactVisibilityRule(
        "executor",
        FIXING,
        "tester",
        TEST_REPORT_ARTIFACTS,
        source_phases=_phases(TESTING, REGRESSION_TESTING),
        round_policy=ROUND_LATEST_COMPLETE_TEST_BEFORE_CURRENT,
    ),
    ArtifactVisibilityRule(
        "executor",
        FIXING,
        "judge",
        JUDGE_DECISION_ARTIFACTS,
        source_phases=_phases(TEST_JUDGEMENT, REVIEW_JUDGEMENT),
        round_policy=ROUND_LATEST_COMPLETE_JUDGE_BEFORE_CURRENT,
    ),
    ArtifactVisibilityRule("executor", REVIEW_FIXING, "orchestrator", PATCH_GATE_ARTIFACTS, round_policy=ROUND_PREVIOUS),
    ArtifactVisibilityRule(
        "executor",
        REVIEW_FIXING,
        "executor",
        _types("merged_patch_metadata.md"),
        source_phases=_phases(PATCH_MERGE),
        round_policy=ROUND_PREVIOUS,
    ),
    ArtifactVisibilityRule(
        "executor",
        REVIEW_FIXING,
        "tester",
        TEST_REPORT_ARTIFACTS,
        source_phases=_phases(TESTING, REGRESSION_TESTING),
        round_policy=ROUND_LATEST_COMPLETE_TEST_BEFORE_CURRENT,
    ),
    ArtifactVisibilityRule(
        "executor",
        REVIEW_FIXING,
        "judge",
        JUDGE_DECISION_ARTIFACTS,
        source_phases=_phases(TEST_JUDGEMENT, REVIEW_JUDGEMENT),
        round_policy=ROUND_LATEST_COMPLETE_JUDGE_BEFORE_CURRENT,
    ),
    ArtifactVisibilityRule(
        "executor",
        REVIEW_FIXING,
        "reviewer",
        _types("review_report.md"),
        source_phases=_phases(REVIEWING),
        round_policy=ROUND_PREVIOUS,
    ),
    ArtifactVisibilityRule("reviewer", REVIEWING, "executor", EXECUTOR_REVIEW_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("reviewer", REVIEWING, "tester", TEST_REPORT_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("reviewer", REVIEWING, "judge", JUDGE_DECISION_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("reviewer", REVIEWING, "orchestrator", PATCH_GATE_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("judge", TEST_JUDGEMENT, "orchestrator", TEST_JUDGE_GATE_ARTIFACTS, round_policy=ROUND_CURRENT),
    ArtifactVisibilityRule(
        "judge",
        TEST_JUDGEMENT,
        "tester",
        TEST_REPORT_ARTIFACTS,
        source_phases=_phases(TESTING, REGRESSION_TESTING),
        round_policy=ROUND_CURRENT,
    ),
    ArtifactVisibilityRule("judge", REVIEW_JUDGEMENT, "orchestrator", PATCH_GATE_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule(
        "judge",
        REVIEW_JUDGEMENT,
        "executor",
        _types("merged_patch_metadata.md"),
        source_phases=_phases(PATCH_MERGE),
        round_policy=ROUND_LATEST_PER_TYPE,
    ),
    ArtifactVisibilityRule("judge", REVIEW_JUDGEMENT, "tester", TEST_REPORT_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule(
        "judge",
        REVIEW_JUDGEMENT,
        "reviewer",
        _types("review_report.md"),
        source_phases=_phases(REVIEWING),
        round_policy=ROUND_CURRENT,
    ),
    ArtifactVisibilityRule("judge", FINAL_JUDGEMENT, "planner", FINAL_PLANNER_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("judge", FINAL_JUDGEMENT, "executor", FINAL_EXECUTOR_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("judge", FINAL_JUDGEMENT, "tester", TEST_REPORT_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("judge", FINAL_JUDGEMENT, "reviewer", FINAL_REVIEWER_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("judge", FINAL_JUDGEMENT, "judge", JUDGE_DECISION_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("judge", FINAL_JUDGEMENT, "orchestrator", PATCH_GATE_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("communicator", DELIVERY, "planner", FINAL_PLANNER_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("communicator", DELIVERY, "executor", FINAL_EXECUTOR_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("communicator", DELIVERY, "tester", TEST_REPORT_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("communicator", DELIVERY, "reviewer", FINAL_REVIEWER_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("communicator", DELIVERY, "judge", JUDGE_DECISION_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
    ArtifactVisibilityRule("communicator", DELIVERY, "orchestrator", PATCH_GATE_ARTIFACTS, round_policy=ROUND_LATEST_PER_TYPE),
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
            if artifact_type == "project_context.md":
                append_once(artifact)
                continue
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
                    latest_matches[(rule_index, artifact_type)] = artifact
                else:
                    append_once(artifact)
                break

        for artifact in latest_matches.values():
            append_once(artifact)
        return filtered

    def rules_for(self, role: str, phase: str) -> list[ArtifactVisibilityRule]:
        return [rule for rule in ARTIFACT_VISIBILITY_RULES if rule.target_role == role and rule.target_phase in {phase, ANY_PHASE}]

    def allowed_by_table(self, target_role: str, target_phase: str, source_role: str, artifact_type: str) -> bool:
        for rule in self.rules_for(target_role, target_phase):
            if rule.source_role == source_role and artifact_type in rule.artifact_types:
                return True
        return False

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
        if (artifact.get("role") or "") != rule.source_role:
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
