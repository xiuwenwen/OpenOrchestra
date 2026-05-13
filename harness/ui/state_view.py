from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from harness.config.user_env import USER_ENV_PATH
from harness.config.runtime import RuntimeConfigService
from harness.core.progress import ProgressEvent
from harness.state.repository import StateRepository
from harness.ui.file_reader import HarnessFileReader


class UiEventStore:
    def __init__(self, max_events_per_task: int = 300):
        self.max_events_per_task = max_events_per_task
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._next_event_id = 0
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=max_events_per_task))
        self._global_events: deque[dict[str, Any]] = deque(maxlen=max_events_per_task * 3)
        self.latest_task_id: str | None = None

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            self._next_event_id += 1
            payload = {
                "id": self._next_event_id,
                "ts": time.time(),
                "event_type": event.event_type,
                "task_id": event.task_id,
                "phase": event.phase,
                "role": event.role,
                "agent_id": event.agent_id,
                "round_id": event.round_id,
                "attempt": event.attempt,
                "status": event.status,
                "message": event.message,
                "trace_id": event.trace_id,
                "span_id": event.span_id,
                "parent_span_id": event.parent_span_id,
                "data": event.data,
            }
            self.latest_task_id = event.task_id
            self._events[event.task_id].append(payload)
            self._global_events.append(payload)
            self._condition.notify_all()

    def events_for(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events.get(task_id, ()))

    def events_since(self, last_event_id: int, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            source = self._events.get(task_id, ()) if task_id else self._global_events
            return [event for event in source if int(event.get("id") or 0) > last_event_id]

    def wait_for_events(self, last_event_id: int, timeout_seconds: float = 15.0) -> list[dict[str, Any]]:
        with self._condition:
            self._condition.wait_for(lambda: self._next_event_id > last_event_id, timeout=timeout_seconds)
            return [event for event in self._global_events if int(event.get("id") or 0) > last_event_id]

    def latest_event_id(self) -> int:
        with self._lock:
            return self._next_event_id

    def select_task(self, task_id: str) -> None:
        with self._lock:
            self.latest_task_id = task_id


class HarnessStateView:
    def __init__(
        self,
        config: dict[str, Any],
        repository: StateRepository,
        event_store: UiEventStore,
        config_path: str | Path | None = None,
        user_env_path: str | Path | None = USER_ENV_PATH,
    ):
        self.config = config
        self.repository = repository
        self.event_store = event_store
        self.config_service = RuntimeConfigService(config, repository, config_path, user_env_path)
        self.file_reader = HarnessFileReader(config)

    def tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(task) for task in self.repository.list_tasks(limit)]

    def get_runtime_config(self) -> dict[str, Any]:
        return self.config_service.role_runtime_config()

    def update_runtime_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.config_service.apply_role_runtime_config(payload)

    def has_active_task(self) -> bool:
        inactive = {"CREATED", "PENDING", "COMPLETED", "FAILED"}
        return any(str(task.get("status") or "") not in inactive for task in self.repository.list_tasks(limit=100))

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        if not task_id:
            task_id = self.event_store.latest_task_id
        if not task_id:
            tasks = self.tasks(1)
            task_id = tasks[0]["task_id"] if tasks else None
        if not task_id:
            return {"task": None, "phases": [], "agent_runs": [], "artifacts": [], "events": []}

        task_record = self.repository.get_task(task_id)
        task = dict(task_record) if task_record else None
        phases = [dict(phase) for phase in self.repository.list_phases(task_id)]
        phase_by_id = {phase["phase_id"]: phase for phase in phases}
        artifacts = [dict(artifact) for artifact in self.repository.list_artifacts(task_id)]
        artifacts_by_run: dict[tuple[str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
        for artifact in artifacts:
            enriched = dict(artifact)
            path = Path(str(artifact["path"]))
            enriched["exists"] = path.exists()
            enriched["size"] = path.stat().st_size if path.exists() else None
            artifacts_by_run[(artifact.get("phase_id"), artifact.get("agent_id"))].append(enriched)

        agent_runs = []
        for run_record in self.repository.list_agent_runs(task_id):
            run = dict(run_record)
            phase = phase_by_id.get(run["phase_id"], {})
            log_dir = self._log_dir_for_run(task_id, phase, run)
            run_artifacts = artifacts_by_run.get((run["phase_id"], run["agent_id"]), [])
            enriched_run = dict(run)
            enriched_run["phase_type"] = phase.get("phase_type")
            enriched_run["phase_round_id"] = phase.get("round_id")
            enriched_run["log_dir"] = str(log_dir)
            enriched_run["prompt_path"] = self._file_info(log_dir / "prompt.md")
            enriched_run["stdout_path"] = self._file_info(log_dir / "stdout.log")
            enriched_run["stderr_path"] = self._file_info(log_dir / "stderr.log")
            enriched_run["diagnostics_path"] = self._file_info(log_dir / "request_diagnostics.md")
            enriched_run["artifacts"] = run_artifacts
            enriched_run["artifact_count"] = len(run_artifacts)
            agent_runs.append(enriched_run)

        success_path = self._delivery_success_path(task)
        task_workspace = self._task_workspace_path(task_id)
        events = self.event_store.events_for(task_id)
        event_log = [dict(event) for event in reversed(self.repository.list_events(task_id, limit=2000))]
        workflow_timeline = self._workflow_timeline(phases)
        return {
            "task": task,
            "phases": phases,
            "workflow_timeline": workflow_timeline,
            "workflow_runs": self._workflow_runs(task, workflow_timeline, event_log),
            "workflow_loop_edges": self._workflow_loop_edges(phases),
            "agent_runs": agent_runs,
            "artifacts": artifacts,
            "events": events,
            "backend_health": self._backend_health(events),
            "event_log": event_log[-200:],
            "roles": self._role_summary(agent_runs, phases),
            "role_rounds": self._role_rounds(agent_runs),
            "success_path": str(success_path) if success_path and success_path.exists() else None,
            "task_workspace": str(task_workspace) if task_workspace.exists() else str(task_workspace),
        }

    def _workflow_timeline(self, phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        timeline: list[dict[str, Any]] = []
        phase_counts: dict[str, int] = defaultdict(int)
        for index, phase in enumerate(phases):
            phase_type = str(phase.get("phase_type") or "-")
            phase_counts[phase_type] += 1
            item = dict(phase)
            item["timeline_index"] = index
            item["phase_occurrence"] = phase_counts[phase_type]
            item["loop_revisit"] = phase_counts[phase_type] > 1
            timeline.append(item)
        return timeline

    def _workflow_loop_edges(self, phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        last_seen: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        for index, phase in enumerate(phases):
            phase_type = str(phase.get("phase_type") or "-")
            previous = last_seen.get(phase_type)
            if previous is not None:
                edges.append(
                    {
                        "phase_type": phase_type,
                        "from_index": previous["index"],
                        "to_index": index,
                        "from_round": previous["round_id"],
                        "to_round": phase.get("round_id"),
                    }
                )
            last_seen[phase_type] = {"index": index, "round_id": phase.get("round_id")}
        return edges

    def _workflow_runs(
        self,
        task: dict[str, Any] | None,
        timeline: list[dict[str, Any]],
        event_log: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not timeline:
            return []
        turns = self._prompt_turns(task)
        starts = [event for event in event_log if event.get("event_type") == "task_started"]
        if not starts:
            return [
                self._workflow_run_item(
                    0,
                    turns[0],
                    timeline,
                    [],
                    task.get("workflow_type") if task else None,
                    task.get("status") if task else None,
                )
            ]
        runs: list[dict[str, Any]] = []
        assigned_phase_ids: set[str] = set()
        for index, start_event in enumerate(starts):
            start_at = str(start_event.get("created_at") or "")
            end_at = str(starts[index + 1].get("created_at") or "") if index + 1 < len(starts) else None
            run_phases = [
                dict(phase, workflow_run_index=index)
                for phase in timeline
                if self._timestamp_in_window(str(phase.get("started_at") or ""), start_at, end_at)
            ]
            for phase in run_phases:
                assigned_phase_ids.add(str(phase.get("phase_id")))
            run_events = [
                event
                for event in event_log
                if self._timestamp_in_window(str(event.get("created_at") or ""), start_at, end_at)
            ]
            payload = self._event_payload(start_event)
            turn = turns[index] if index < len(turns) else self._prompt_turn(index, "")
            runs.append(
                self._workflow_run_item(
                    index,
                    turn,
                    run_phases,
                    run_events,
                    payload.get("workflow_type") or (task.get("workflow_type") if task else None),
                    task.get("status") if task and index == len(starts) - 1 else None,
                )
            )
        unassigned = [
            dict(phase, workflow_run_index=0)
            for phase in timeline
            if str(phase.get("phase_id")) not in assigned_phase_ids
        ]
        if unassigned and runs:
            runs[0]["phases"] = unassigned + runs[0]["phases"]
            runs[0]["phase_count"] = len(runs[0]["phases"])
        return runs

    def _workflow_run_item(
        self,
        index: int,
        turn: dict[str, Any],
        phases: list[dict[str, Any]],
        events: list[dict[str, Any]],
        workflow_type: Any,
        fallback_status: Any,
    ) -> dict[str, Any]:
        return {
            "run_index": index,
            "turn_index": turn["turn_index"],
            "prompt": turn["prompt"],
            "workflow_type": workflow_type or "-",
            "status": self._workflow_run_status(events, fallback_status),
            "phase_count": len(phases),
            "phases": phases,
        }

    def _workflow_run_status(self, events: list[dict[str, Any]], fallback_status: Any) -> str:
        for event in reversed(events):
            if event.get("event_type") == "task_completed":
                return "COMPLETED"
            if event.get("event_type") == "task_failed":
                return "FAILED"
        return str(fallback_status or "RUNNING")

    def _prompt_turns(self, task: dict[str, Any] | None) -> list[dict[str, Any]]:
        prompt = str(task.get("user_prompt") or "") if task else ""
        parts = prompt.split("\n\nFollow-up request:\n")
        turns = [self._prompt_turn(index, part.strip()) for index, part in enumerate(parts) if part.strip()]
        return turns or [self._prompt_turn(0, "")]

    def _prompt_turn(self, index: int, prompt: str) -> dict[str, Any]:
        return {"turn_index": index, "prompt": prompt}

    def _timestamp_in_window(self, timestamp: str, start_at: str, end_at: str | None) -> bool:
        if not timestamp:
            return False
        if start_at and timestamp < start_at:
            return False
        return not end_at or timestamp < end_at

    def _event_payload(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload")
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str) and payload.strip():
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _backend_health(self, events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        health: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.get("event_type") not in {"backend_health_changed", "backend_circuit_open"}:
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            backend = str(data.get("backend") or "")
            if not backend:
                continue
            health[backend] = {
                "backend": backend,
                "state": data.get("backend_health_state") or str(event.get("status") or "").lower(),
                "allowed": data.get("backend_health_allowed"),
                "consecutive_failures": data.get("backend_consecutive_failures"),
                "failure_kind": data.get("backend_failure_kind"),
                "message": event.get("message"),
            }
        return health

    def read_file(self, path_text: str, max_chars: int = 200_000) -> dict[str, Any]:
        return self.file_reader.read_file(path_text, max_chars=max_chars)

    def _log_dir_for_run(self, task_id: str, phase: dict[str, Any], run: dict[str, Any]) -> Path:
        workspace_root = Path(self.config["system"]["workspace_root"]).expanduser().resolve()
        round_id = phase.get("round_id", 0)
        return (
            workspace_root
            / task_id
            / str(run["phase_id"])
            / str(run["role"])
            / str(run["agent_id"])
            / f"round_{round_id}"
            / f"attempt_{run['retry_count']}"
            / "logs"
        )

    def _task_workspace_path(self, task_id: str) -> Path:
        workspace_root = Path(self.config["system"]["workspace_root"]).expanduser().resolve()
        return workspace_root / task_id

    def _file_info(self, path: Path) -> dict[str, Any]:
        return {
            "path": str(path),
            "exists": path.exists() and path.is_file(),
            "size": path.stat().st_size if path.exists() and path.is_file() else None,
        }

    def _role_summary(self, agent_runs: list[dict[str, Any]], phases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        phase_by_role: dict[str, dict[str, Any]] = {}
        for phase in phases:
            phase_by_role[str(phase["role"])] = phase
        summary: dict[str, dict[str, Any]] = {}
        for role in ("planner", "executor", "tester", "reviewer", "judge", "communicator", "orchestrator"):
            role_runs = [run for run in agent_runs if run["role"] == role]
            latest_run = role_runs[-1] if role_runs else None
            phase = phase_by_role.get(role)
            summary[role] = {
                "role": role,
                "status": latest_run["status"] if latest_run else (phase["status"] if phase else "PENDING"),
                "phase": latest_run.get("phase_type") if latest_run else (phase["phase_type"] if phase else "-"),
                "agent_count": len({run["agent_id"] for run in role_runs}),
                "artifact_count": sum(int(run.get("artifact_count", 0)) for run in role_runs),
            }
        return summary

    def _role_rounds(self, agent_runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, dict[tuple[int, str], dict[str, Any]]] = defaultdict(dict)
        for run in agent_runs:
            role = str(run["role"])
            round_id = int(run.get("phase_round_id") or 0)
            phase_type = str(run.get("phase_type") or "-")
            key = (round_id, phase_type)
            role_round = grouped[role].setdefault(
                key,
                {"role": role, "round_id": round_id, "phase_type": phase_type, "runs": [], "artifact_count": 0},
            )
            role_round["runs"].append(run)
            role_round["artifact_count"] += int(run.get("artifact_count", 0))
        result: dict[str, list[dict[str, Any]]] = {}
        for role, rounds in grouped.items():
            result[role] = sorted(rounds.values(), key=lambda item: (item["round_id"], item["phase_type"]))
        return result

    def _delivery_success_path(self, task: dict[str, Any] | None) -> Path | None:
        if not task:
            return None
        task_id = str(task["task_id"])
        for artifact in reversed(self.repository.list_artifacts(task_id, "success_path.md")):
            path = Path(str(artifact["path"])).expanduser().resolve()
            if path.exists() and path.is_file():
                return path.parent
        deliver_root = Path(self.config["system"].get("deliver_root", "./deliver")).expanduser().resolve()
        matches = sorted(deliver_root.glob(f"*-{task_id[:8]}/success_path.md"))
        return matches[-1].parent if matches else None
