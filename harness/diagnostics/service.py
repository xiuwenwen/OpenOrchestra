from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.state.repository import StateRepository


SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)(\s*[:=]\s*)([^\s`'\",]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{12,})"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})\b"),
)


@dataclass(frozen=True)
class DiagnosticsBundle:
    task_id: str
    path: Path
    copied_files: int


class DiagnosticsService:
    def __init__(self, *, config: dict[str, Any], repository: StateRepository):
        self.config = config
        self.repository = repository

    def export_task(self, task_id: str, output_root: Path | None = None) -> DiagnosticsBundle:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        bundle_dir = self._bundle_dir(task_id, output_root)
        copied_files = 0
        bundle_dir.mkdir(parents=True, exist_ok=False)
        (bundle_dir / "state").mkdir()
        (bundle_dir / "artifacts").mkdir()
        (bundle_dir / "runs").mkdir()

        phases = [dict(row) for row in self.repository.list_phases(task_id)]
        phase_by_id = {str(phase["phase_id"]): phase for phase in phases}
        runs = [dict(row) for row in self.repository.list_agent_runs(task_id)]
        artifacts = [dict(row) for row in self.repository.list_artifacts(task_id)]
        events = [dict(row) for row in reversed(self.repository.list_events(task_id, limit=1000))]

        self._write_json(bundle_dir / "state" / "task.json", dict(task))
        self._write_json(bundle_dir / "state" / "phases.json", phases)
        self._write_json(bundle_dir / "state" / "agent_runs.json", runs)
        self._write_json(bundle_dir / "state" / "artifacts.json", artifacts)
        self._write_json(bundle_dir / "state" / "events.json", events)
        self._write_timeline(bundle_dir / "timeline.md", events)

        for index, artifact in enumerate(artifacts, start=1):
            if self._copy_registered_artifact(bundle_dir / "artifacts", index, artifact):
                copied_files += 1

        for run in runs:
            copied_files += self._copy_run_evidence(bundle_dir / "runs", task_id, run, phase_by_id)

        self._write_summary(bundle_dir / "summary.md", dict(task), phases, runs, artifacts, events, copied_files)
        return DiagnosticsBundle(task_id=task_id, path=bundle_dir, copied_files=copied_files)

    def _bundle_dir(self, task_id: str, output_root: Path | None) -> Path:
        root = output_root or Path(self.config["system"].get("diagnostics_root", "logs/diagnostics"))
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return root.expanduser().resolve() / f"{task_id}-{timestamp}"

    def _copy_registered_artifact(self, artifact_dir: Path, index: int, artifact: dict[str, Any]) -> bool:
        source = Path(str(artifact.get("path") or ""))
        if not source.exists() or not source.is_file():
            return False
        target = artifact_dir / f"{index:03d}_{self._safe_name(artifact)}"
        self._copy_text_with_redaction(source, target)
        return True

    def _copy_run_evidence(
        self,
        runs_dir: Path,
        task_id: str,
        run: dict[str, Any],
        phase_by_id: dict[str, dict[str, Any]],
    ) -> int:
        phase = phase_by_id.get(str(run.get("phase_id") or ""), {})
        round_id = int(phase.get("round_id") or 0)
        run_dir = (
            runs_dir
            / self._safe_token(str(phase.get("phase_type") or "phase"))
            / self._safe_token(str(run.get("role") or "role"))
            / self._safe_token(str(run.get("agent_id") or "agent"))
            / f"round_{round_id}"
            / f"attempt_{run.get('retry_count') or 0}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        source_root = (
            Path(self.config["system"]["workspace_root"]).expanduser().resolve()
            / task_id
            / str(run["phase_id"])
            / str(run["role"])
            / str(run["agent_id"])
            / f"round_{round_id}"
            / f"attempt_{run['retry_count']}"
        )
        copied = 0
        for relative in (
            Path("logs") / "prompt.md",
            Path("logs") / "stdout.log",
            Path("logs") / "stderr.log",
            Path("logs") / "request_diagnostics.md",
            Path("input") / "manifest.md",
        ):
            source = source_root / relative
            if not source.exists() or not source.is_file():
                continue
            target = run_dir / relative.name
            self._copy_text_with_redaction(source, target)
            copied += 1
        return copied

    def _write_summary(
        self,
        path: Path,
        task: dict[str, Any],
        phases: list[dict[str, Any]],
        runs: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        events: list[dict[str, Any]],
        copied_files: int,
    ) -> None:
        lines = [
            "# OpenOrchestra Diagnostics Bundle",
            "",
            f"- task_id: `{task.get('task_id')}`",
            f"- status: `{task.get('status')}`",
            f"- workflow_type: `{task.get('workflow_type') or '-'}`",
            f"- current_phase: `{task.get('current_phase') or '-'}`",
            f"- current_role: `{task.get('current_role') or '-'}`",
            f"- phases: `{len(phases)}`",
            f"- agent_runs: `{len(runs)}`",
            f"- artifacts: `{len(artifacts)}`",
            f"- events: `{len(events)}`",
            f"- copied_evidence_files: `{copied_files}`",
            "",
            "## Contents",
            "",
            "- `state/*.json`: task, phase, run, artifact, and event records",
            "- `timeline.md`: chronological event timeline",
            "- `artifacts/`: registered artifacts copied with text redaction",
            "- `runs/`: prompt, stdout, stderr, request diagnostics, and input manifest per agent run",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_timeline(self, path: Path, events: list[dict[str, Any]]) -> None:
        lines = ["# Event Timeline", ""]
        for event in events:
            lines.append(
                "- "
                + " | ".join(
                    [
                        str(event.get("created_at") or "-"),
                        str(event.get("event_type") or "-"),
                        str(event.get("phase") or "-"),
                        str(event.get("role") or "-"),
                        str(event.get("agent_id") or "-"),
                        str(event.get("status") or "-"),
                        self._redact_text(str(event.get("message") or "-")),
                    ]
                )
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_json(self, path: Path, payload: Any) -> None:
        path.write_text(json.dumps(self._redact_payload(payload), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    def _copy_text_with_redaction(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            shutil.copy2(source, target)
            return
        target.write_text(self._redact_text(text), encoding="utf-8")

    def _redact_payload(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            return {key: self._redact_payload(value) for key, value in payload.items()}
        if isinstance(payload, list):
            return [self._redact_payload(value) for value in payload]
        if isinstance(payload, str):
            return self._redact_text(payload)
        return payload

    def _redact_text(self, text: str) -> str:
        redacted = text
        redacted = SECRET_PATTERNS[0].sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
        redacted = SECRET_PATTERNS[1].sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
        redacted = SECRET_PATTERNS[2].sub("sk-[REDACTED]", redacted)
        return redacted

    def _safe_name(self, artifact: dict[str, Any]) -> str:
        role = self._safe_token(str(artifact.get("role") or "unknown"))
        agent = self._safe_token(str(artifact.get("agent_id") or "unknown"))
        artifact_type = self._safe_token(str(artifact.get("artifact_type") or "artifact"))
        return f"{role}_{agent}_{artifact_type}"

    def _safe_token(self, value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return safe or "unknown"
