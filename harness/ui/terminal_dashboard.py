from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field

from harness.core.progress import ProgressEvent
from harness.ui.display import display_width, pad_display, truncate_display
from harness.ui.terminal import TerminalStatusLine


class ConsoleProgressReporter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            line = self._format(event)
            print(line, flush=True)

    def _format(self, event: ProgressEvent) -> str:
        parts = ["[progress]", event.event_type]
        if event.phase:
            parts.append(f"phase={event.phase}")
        if event.role:
            parts.append(f"role={event.role}")
        if event.agent_id:
            parts.append(f"agent={event.agent_id}")
        if event.round_id is not None:
            parts.append(f"round={event.round_id}")
        if event.attempt is not None:
            parts.append(f"attempt={event.attempt + 1}")
        if event.status:
            parts.append(f"status={event.status}")
        for key in (
            "backend",
            "workspace",
            "output",
            "logs",
            "diagnostics",
            "artifacts",
            "result_type",
            "result_path",
            "success_path",
            "elapsed_seconds",
        ):
            if key in event.data:
                parts.append(f"{key}={event.data[key]}")
        if "delivery_status" in event.data:
            parts.append(f"delivery_status={event.data['delivery_status']}")
        if event.message:
            parts.append(f"- {event.message}")
        return " ".join(parts)


@dataclass
class RoleView:
    role: str
    status: str = "PENDING"
    phase: str = "-"
    round_id: int | None = None
    agent_id: str = "-"
    attempt: int | None = None
    artifacts: int = 0
    backend: str = "-"
    message: str = ""
    delivery_status: str = "-"
    elapsed_seconds: float | None = None


@dataclass
class DashboardState:
    task_id: str = "-"
    task_status: str = "-"
    current_phase: str = "-"
    backend: str = "-"
    workflow_type: str = "-"
    test_round: int = 0
    review_round: int = 0
    result_path: str = "-"
    success_path: str = "-"
    result_type: str = "-"
    roles: dict[str, RoleView] = field(default_factory=dict)
    agents: dict[str, RoleView] = field(default_factory=dict)


class DashboardProgressReporter(ConsoleProgressReporter):
    ROLES = ("planner", "executor", "tester", "reviewer", "judge", "communicator", "orchestrator")

    def __init__(self) -> None:
        super().__init__()
        self.state = DashboardState()
        for role in self.ROLES:
            self.state.roles[role] = RoleView(role=role)
        self.enabled = sys.stdout.isatty()
        self._rendered_lines = 0

    def __call__(self, event: ProgressEvent) -> None:
        if not self.enabled:
            return super().__call__(event)
        with self._lock:
            event_line = self._event_line(event)
            self._apply(event)
            self._render(event_line=event_line)

    def _apply(self, event: ProgressEvent) -> None:
        if event.event_type == "task_started" and event.task_id:
            self._reset_for_task(event.task_id)
        elif event.event_type == "task_created" and event.task_id and event.task_id != self.state.task_id:
            self._reset_for_task(event.task_id)
        self.state.task_id = event.task_id or self.state.task_id
        if event.phase:
            self.state.current_phase = event.phase
        if event.status:
            self.state.task_status = event.status if event.event_type.startswith("task_") else self.state.task_status
        if event.event_type == "phase_skipped":
            role = event.role
            if role and role in self.state.roles:
                row = self.state.roles[role]
                row.status = "COMPLETED"
                row.phase = event.phase
                row.round_id = event.round_id if event.round_id is not None else row.round_id
                row.message = event.message or "Skipped (already completed)"
            return
        if "backend" in event.data:
            self.state.backend = str(event.data["backend"])
        if "workflow_type" in event.data:
            self.state.workflow_type = str(event.data["workflow_type"])
        if (
            event.phase
            in {
                "PLANNING_DRAFT",
                "PLANNING_PEER_REVIEW",
                "PLANNING_REVISION",
                "PLAN_REVIEW",
                "PLAN_JUDGEMENT",
                "TESTING",
                "TEST_JUDGEMENT",
                "FIXING",
                "PATCH_MERGE",
                "EXECUTION",
                "REGRESSION_TESTING",
                "REVIEW_FIXING",
            }
            and event.round_id is not None
        ):
            self.state.test_round = event.round_id
        if event.phase in {"REVIEWING", "REVIEW_JUDGEMENT", "REVIEW_FIXING", "REGRESSION_TESTING"} and event.round_id is not None:
            self.state.review_round = event.round_id
        if "result_path" in event.data:
            self.state.result_path = str(event.data["result_path"])
        if "success_path" in event.data:
            self.state.success_path = str(event.data["success_path"])
        if "result_type" in event.data:
            self.state.result_type = str(event.data["result_type"])
        role = event.role
        if role and role in self.state.roles:
            row = self.state.roles[role]
            is_phase_event = event.event_type.startswith("phase_")
            self._apply_row(
                row,
                event,
                aggregate_artifacts=True,
                update_status=is_phase_event or row.status == "PENDING",
                update_identity=False,
            )
            if event.agent_id:
                agent_key = f"{role}:{event.agent_id}"
                agent_row = self.state.agents.setdefault(agent_key, RoleView(role=f"  {event.agent_id}"))
                self._apply_row(agent_row, event, aggregate_artifacts=False, update_status=True)

    def _reset_for_task(self, task_id: str) -> None:
        self.state = DashboardState(task_id=task_id)
        for role in self.ROLES:
            self.state.roles[role] = RoleView(role=role)

    def _apply_row(
        self,
        row: RoleView,
        event: ProgressEvent,
        *,
        aggregate_artifacts: bool,
        update_status: bool = True,
        update_identity: bool = True,
    ) -> None:
        if update_status:
            row.status = event.status or row.status
            row.phase = event.phase or row.phase
            row.round_id = event.round_id if event.round_id is not None else row.round_id
        if update_identity:
            row.agent_id = event.agent_id or row.agent_id
            row.attempt = event.attempt + 1 if event.attempt is not None else row.attempt
        row.backend = str(event.data.get("backend", row.backend))
        if "artifacts" in event.data:
            if aggregate_artifacts:
                row.artifacts += int(event.data["artifacts"])
            else:
                row.artifacts = int(event.data["artifacts"])
        if "delivery_status" in event.data:
            row.delivery_status = str(event.data["delivery_status"])
        if "elapsed_seconds" in event.data and event.event_type in {
            "agent_completed",
            "agent_retryable_failure",
            "agent_failed",
            "phase_completed",
            "phase_failed",
            "agent_heartbeat",
        }:
            row.elapsed_seconds = float(event.data["elapsed_seconds"])
        row.message = event.message or row.message

    def _render(self, event_line: str | None = None) -> None:
        lines = self._dashboard_lines()
        TerminalStatusLine.clear()
        if self._rendered_lines:
            sys.stdout.write(f"\x1b[{self._rendered_lines}F")
        if event_line:
            sys.stdout.write(f"\x1b[2K{event_line}\n")
        for line in lines:
            sys.stdout.write(f"\x1b[2K{line}\n")
        surplus_lines = max(0, self._rendered_lines - len(lines) - (1 if event_line else 0))
        for _ in range(surplus_lines):
            sys.stdout.write("\x1b[2K\n")
        if surplus_lines:
            sys.stdout.write(f"\x1b[{surplus_lines}F")
        self._rendered_lines = len(lines)
        sys.stdout.flush()

    def _dashboard_lines(self) -> list[str]:
        lines = [
            "OpenOrchestra Execution Dashboard",
            "=" * 88,
            f"Task: {self.state.task_id}",
            (
                f"Status: {self.state.task_status}   Phase: {self.state.current_phase}   "
                f"Backend: {self.state.backend}   Workflow: {self.state.workflow_type}"
            ),
            f"Test/Fix round: {self.state.test_round}   Review round: {self.state.review_round}",
            "-" * 88,
            (
                f"{'Role':<14}{'Status':<14}{'Phase':<20}{'Round':<8}{'Agent':<18}"
                f"{'Try':<6}{'Artifacts':<10}{'Delivery':<10}{'Elapsed':<10}"
            ),
            "-" * 88,
        ]
        for role in self.ROLES:
            row = self.state.roles[role]
            lines.append(self._format_row(row))
            for agent_row in self._agent_rows_for_role(role):
                lines.append(self._format_row(agent_row))
        lines.append("-" * 88)
        if self.state.result_path != "-":
            label = "response" if self.state.result_type == "response" else "result"
            lines.append(f"{label}: {self.state.result_path}")
        return lines

    def _event_line(self, event: ProgressEvent) -> str:
        if event.event_type == "agent_heartbeat":
            return self._compact_line(event, prefix="[running]")
        return self._compact_line(event)

    def _compact_line(self, event: ProgressEvent, *, prefix: str | None = None) -> str:
        event_prefixes = {
            "task_created": "[task]",
            "task_started": "[task]",
            "task_completed": "[ok]",
            "task_failed": "[fail]",
            "phase_started": "[phase]",
            "phase_completed": "[ok]",
            "phase_failed": "[fail]",
            "phase_skipped": "[skip]",
            "agent_started": "[agent]",
            "agent_completed": "[ok]",
            "agent_retryable_failure": "[retry]",
            "agent_failed": "[fail]",
            "patch_validated": "[gate]",
        }
        parts = [prefix or event_prefixes.get(event.event_type, "[progress]")]
        if event.phase:
            parts.append(event.phase)
        if event.role:
            parts.append(event.role)
        if event.agent_id:
            parts.append(event.agent_id)
        if event.round_id is not None:
            parts.append(f"round={event.round_id}")
        if event.attempt is not None:
            parts.append(f"try={event.attempt + 1}")
        if event.status:
            parts.append(str(event.status))
        for key in ("workflow_type", "backend", "artifacts", "delivery_status", "elapsed_seconds"):
            if key in event.data:
                value = event.data[key]
                if key == "elapsed_seconds":
                    value = f"{float(value):.1f}s"
                parts.append(f"{key}={value}")
        if event.message:
            parts.append(f"- {event.message}")
        return truncate_display(" ".join(parts), 120)

    def _render_row(self, row: RoleView) -> None:
        sys.stdout.write(self._format_row(row) + "\n")

    def _format_row(self, row: RoleView) -> str:
        round_text = "-" if row.round_id is None else str(row.round_id)
        attempt_text = "-" if row.attempt is None else str(row.attempt)
        elapsed_text = "-" if row.elapsed_seconds is None else f"{row.elapsed_seconds:.3f}s"
        return (
            f"{pad_display(row.role, 14)}{pad_display(row.status, 14)}{pad_display(row.phase, 20)}{pad_display(round_text, 8)}"
            f"{pad_display(row.agent_id, 18)}{pad_display(attempt_text, 6)}{pad_display(str(row.artifacts), 10)}"
            f"{pad_display(row.delivery_status, 10)}{pad_display(elapsed_text, 10)}"
        )

    def _agent_rows_for_role(self, role: str) -> list[RoleView]:
        prefix = f"{role}:"
        return [self.state.agents[key] for key in sorted(self.state.agents) if key.startswith(prefix)]

    def _display_width(self, text: str) -> int:
        return display_width(text)


def make_progress_reporter() -> ConsoleProgressReporter:
    return DashboardProgressReporter() if sys.stdout.isatty() else ConsoleProgressReporter()
