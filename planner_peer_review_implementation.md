# Planner Peer Review Implementation

## Goal

Rework planning so multiple planner agents do not only draft in parallel. Harness now coordinates a bounded peer-review loop through artifacts before execution starts.

This flow is a planning-specific instance of the generic collaboration protocol:

```text
PROPOSE -> CRITIQUE -> REVISE -> VOTE -> MERGE
```

The same protocol can be applied to executor collaboration: one executor proposes a patch, another critiques it with artifact-backed evidence, the patch author revises, agents vote or block, and the reviewer/judge or PATCH_MERGE step selects or merges the final candidate before objective gates validate it.

## Flow

1. `PLANNING_DRAFT`: each planner writes its initial `plan.md`, `assumptions.md`, `risk.md`, and `todo_breakdown.json`.
2. `PLANNING_PEER_REVIEW`: each planner reviews other planners' proposals and writes `peer_review_result.json`.
3. `PLANNING_REVISION`: planners read peer feedback and revise their own planning artifacts.
4. Repeat peer review and revision up to `limits.planning_peer_review_loops`, default 3.
5. `PLAN_REVIEW`: reviewer reads all plans and peer reviews, then writes `review_result.json` and `selected_plan.json`.
6. `PLAN_JUDGEMENT`: judge approves or rejects the selected planning basis.
7. Execution starts only after planning approval.

## Notes

- Agents still do not directly communicate. Notifications and review material are represented by staged artifacts.
- `peer_review_result.json` must include `peer_review_status: satisfied` or `changes_requested`.
- Executor input visibility now includes planner peer reviews and the reviewer-selected planning report.
- Communicator prompts now require final delivery to include success path, source/project path, and exact run commands.

## Validation

Ran:

```bash
.venv/bin/python -m pytest
```

Result: `103 passed`.
