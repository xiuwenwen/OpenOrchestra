# Planner Peer Review Implementation

## Goal

Rework planning so multiple planner agents do not only draft in parallel. Harness now coordinates a bounded peer-review loop through artifacts before execution starts.

## Flow

1. `PLANNING_DRAFT`: each planner writes its initial `plan.md`, `assumptions.md`, `risk.md`, and `todo_breakdown.md`.
2. `PLANNING_PEER_REVIEW`: each planner reviews other planners' proposals and writes `peer_review.md`.
3. `PLANNING_REVISION`: planners read peer feedback and revise their own planning artifacts.
4. Repeat peer review and revision up to `limits.planning_peer_review_loops`, default 3.
5. `PLAN_REVIEW`: reviewer reads all plans and peer reviews, then writes `review_report.md` selecting the best planner proposal by `agent_id`.
6. `PLAN_JUDGEMENT`: judge approves or rejects the selected planning basis.
7. Execution starts only after planning approval.

## Notes

- Agents still do not directly communicate. Notifications and review material are represented by staged artifacts.
- `peer_review.md` must include `status: satisfied` or `status: changes_requested`.
- Executor input visibility now includes planner peer reviews and the reviewer-selected planning report.
- Communicator prompts now require final delivery to include success path, source/project path, and exact run commands.

## Validation

Ran:

```bash
.venv/bin/python -m pytest
```

Result: `103 passed`.
