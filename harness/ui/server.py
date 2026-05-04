from __future__ import annotations

import json
import re
import shutil
import threading
import time
from hashlib import sha256
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from harness.core.misc_chat import MiscChatRunner
from harness.core.progress import ProgressEvent
from harness.state.repository import StateRepository


class UiEventStore:
    def __init__(self, max_events_per_task: int = 300):
        self.max_events_per_task = max_events_per_task
        self._lock = threading.RLock()
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=max_events_per_task))
        self.latest_task_id: str | None = None

    def __call__(self, event: ProgressEvent) -> None:
        payload = {
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
            "data": event.data,
        }
        with self._lock:
            self.latest_task_id = event.task_id
            self._events[event.task_id].append(payload)

    def events_for(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events.get(task_id, ()))

    def select_task(self, task_id: str) -> None:
        with self._lock:
            self.latest_task_id = task_id


class HarnessStateView:
    def __init__(self, config: dict[str, Any], repository: StateRepository, event_store: UiEventStore):
        self.config = config
        self.repository = repository
        self.event_store = event_store

    def tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.repository.list_tasks(limit)

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        if not task_id:
            task_id = self.event_store.latest_task_id
        if not task_id:
            tasks = self.tasks(1)
            task_id = tasks[0]["task_id"] if tasks else None
        if not task_id:
            return {"task": None, "phases": [], "agent_runs": [], "artifacts": [], "events": []}

        task = self.repository.get_task(task_id)
        phases = self.repository.list_phases(task_id)
        phase_by_id = {phase["phase_id"]: phase for phase in phases}
        artifacts = self.repository.list_artifacts(task_id)
        artifacts_by_run: dict[tuple[str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
        for artifact in artifacts:
            enriched = dict(artifact)
            path = Path(str(artifact["path"]))
            enriched["exists"] = path.exists()
            enriched["size"] = path.stat().st_size if path.exists() else None
            artifacts_by_run[(artifact.get("phase_id"), artifact.get("agent_id"))].append(enriched)

        agent_runs = []
        for run in self.repository.list_agent_runs(task_id):
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
        return {
            "task": task,
            "phases": phases,
            "agent_runs": agent_runs,
            "artifacts": artifacts,
            "events": self.event_store.events_for(task_id),
            "roles": self._role_summary(agent_runs, phases),
            "role_rounds": self._role_rounds(agent_runs),
            "success_path": str(success_path) if success_path and success_path.exists() else None,
            "task_workspace": str(task_workspace) if task_workspace.exists() else str(task_workspace),
        }

    def read_file(self, path_text: str, max_chars: int = 200_000) -> dict[str, Any]:
        path = Path(unquote(path_text)).expanduser().resolve()
        if not self._is_allowed_path(path):
            raise PermissionError(f"Path is outside Harness readable roots: {path}")
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))
        raw = path.read_text(encoding="utf-8", errors="replace")
        truncated = len(raw) > max_chars
        text = raw[-max_chars:] if truncated else raw
        return {
            "path": str(path),
            "size": path.stat().st_size,
            "text": text,
            "truncated_from_start": truncated,
        }

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

    def _is_allowed_path(self, path: Path) -> bool:
        roots = [
            self.config["system"].get("workspace_root", "./workspaces"),
            self.config["system"].get("artifact_root", "./artifacts"),
            self.config["system"].get("deliver_root", "./deliver"),
            "logs",
        ]
        resolved_roots = [Path(str(root)).expanduser().resolve() for root in roots]
        return any(_is_relative_to(path, root) for root in resolved_roots)


class DisplayTranslator:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, str]] = {}

    def translate_to_zh(self, text: str) -> dict[str, str]:
        key = sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._cache.get(key)
        if cached:
            return cached

        fallback = self._fallback_translate(text)
        backend = self._translation_backend()
        if not backend:
            result = {"text": fallback, "mode": "fallback", "error": "translation backend unavailable"}
            with self._lock:
                self._cache[key] = result
            return result

        try:
            translated = self._model_translate(text, backend)
            result = {"text": translated or fallback, "mode": "model" if translated else "fallback", "error": ""}
        except Exception as exc:
            result = {"text": fallback, "mode": "fallback", "error": str(exc)}
        with self._lock:
            self._cache[key] = result
        return result

    def _translation_backend(self) -> str | None:
        backend = str(self.config.get("agent_backend", {}).get("default", "") or "")
        if backend in {"claude", "codex"} and shutil.which(backend):
            return backend
        for candidate in ("claude", "codex"):
            if shutil.which(candidate):
                return candidate
        return None

    def _model_translate(self, text: str, backend: str) -> str:
        masked_text, placeholders = self._mask_markdown_for_translation(text)
        chunks = self._chunks(masked_text, max_chars=12000)
        translated_chunks = []
        runner = MiscChatRunner(backend, log_root="logs/ui_translate", config=self.config)
        for chunk in chunks:
            prompt = "\n".join(
                [
                    "Translate the following Harness UI display text into Simplified Chinese.",
                    "Preserve Markdown structure.",
                    "Do not translate placeholders like __KEEP_0__.",
                    "Do not translate file paths, commands, code, JSON, YAML, environment variables, or configuration keys.",
                    "Translate only prose, prompts, explanations, role instructions, and model-visible statements.",
                    "Return only the translated text, without commentary.",
                    "",
                    chunk,
                ]
            )
            translated_chunks.append(runner.ask(prompt, timeout_seconds=0).strip())
        return self._restore_placeholders("\n".join(translated_chunks), placeholders)

    def _mask_markdown_for_translation(self, text: str) -> tuple[str, list[str]]:
        in_fence = False
        masked_lines: list[str] = []
        placeholders: list[str] = []
        for line in text.splitlines():
            if re.match(r"^\s*```", line):
                in_fence = not in_fence
                masked_lines.append(line)
                continue
            if in_fence or self._should_preserve_line(line):
                masked_lines.append(line)
                continue
            masked_lines.append(self._mask_inline_preserved_tokens(line, placeholders))
        return "\n".join(masked_lines), placeholders

    def _mask_inline_preserved_tokens(self, line: str, placeholders: list[str]) -> str:
        def keep(match: re.Match[str]) -> str:
            marker = f"__KEEP_{len(placeholders)}__"
            placeholders.append(match.group(0))
            return marker

        return re.sub(
            r"`[^`]*`|https?://\S+|(?:/|~/|\.\.?/)[^\s),;]+|[A-Za-z0-9_.-]+\.(?:md|py|js|ts|tsx|json|yaml|yml|txt|log|diff|patch|html|css|sh)\b",
            keep,
            line,
        )

    def _restore_placeholders(self, text: str, placeholders: list[str]) -> str:
        for index, token in enumerate(placeholders):
            text = text.replace(f"__KEEP_{index}__", token)
        return text

    def _should_preserve_line(self, line: str) -> bool:
        trimmed = line.strip()
        if not trimmed:
            return True
        if re.match(r"^(diff --git|index |--- |\+\+\+ |@@ |[+-]{3,})", trimmed):
            return True
        if re.match(r"^[+-]\s", trimmed) and re.search(r"[`$./\\]|^\+\s*(import|from|def|class|const|let|var|function)\b", trimmed):
            return True
        if re.match(r"^(curl|python3?|pip|npm|pnpm|yarn|bun|uv|pytest|git|docker|make|cargo|go|node|claude|codex|source|cd|mkdir|cp|mv|rm|cat|sed|rg|grep|ls|open)\b", trimmed):
            return True
        if re.match(r"^\$ ", trimmed):
            return True
        if re.match(r"^(https?://|file://)", trimmed):
            return True
        if re.match(r"^(/|~/|\.\.?/)[^\s]*$", trimmed):
            return True
        if re.match(r"^[A-Za-z]:[\\/]", trimmed):
            return True
        if re.match(r"^[-*]\s+(`[^`]+`|/|~/|\.\.?/|https?://)", trimmed):
            return True
        if re.match(r"^\s*[{[\]}],?\s*$", line):
            return True
        if re.match(r'^\s*"[^"]+"\s*:\s*("[^"]*"|\d+|true|false|null|[{[]),?\s*$', line):
            return True
        if re.match(r"^\s*[A-Z0-9_]+\s*=", line):
            return True
        if re.match(r"^\s*[-*]\s+[A-Za-z0-9_.\/~-]+\.(md|py|js|ts|tsx|json|yaml|yml|txt|log|diff|patch|html|css|sh)\b", line):
            return True
        return False

    def _chunks(self, text: str, max_chars: int) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in text.splitlines():
            line_len = len(line) + 1
            if current and current_len + line_len > max_chars:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len
        if current:
            chunks.append("\n".join(current))
        return chunks or [""]

    def _fallback_translate(self, text: str) -> str:
        replacements = [
            (r"Workflow classification: new_project\.", "工作流分类：新项目。"),
            (r"Use the full new-project workflow from planning through final delivery\.", "使用完整的新项目工作流，从规划一直到最终交付。"),
            (r"Establish project structure, implementation approach, validation strategy, and final handoff artifacts\.", "建立项目结构、实现方案、验证策略和最终移交产物。"),
            (r"Original user prompt:", "原始用户提示词："),
            (r"Create planning artifacts only\.", "只创建规划产物。"),
            (r"Analyze the request, existing artifacts, assumptions, risks, compatibility constraints, and an actionable task breakdown\.", "分析请求、现有产物、假设、风险、兼容性约束，并给出可执行的任务拆解。"),
            (r"Do not modify source files\.", "不要修改源码文件。"),
            (r"Your delivery\.md status must be 'success' if you produced a complete plan, even if you identify high risks\.", "如果你产出了完整计划，即使识别出高风险，delivery.md 的状态也必须是 'success'。"),
            (r"Specialization: Pragmatic Planner\.", "专长：务实规划者。"),
            (r"Preference: MVP-first, implementation-oriented, complexity-minimizing\.", "偏好：MVP 优先、面向实现、最小化复杂度。"),
            (r"Focus:", "关注点："),
            (r"Role Responsibility", "角色职责"),
            (r"Role Specialization", "角色专长"),
            (r"User Request", "用户请求"),
            (r"Implementation", "实现"),
            (r"approach", "方案"),
            (r"complete", "完整"),
            (r"success", "成功"),
        ]
        out = text
        for pattern, value in replacements:
            out = re.sub(pattern, value, out, flags=re.IGNORECASE)
        return out


class HarnessWebServer:
    def __init__(
        self,
        config: dict[str, Any],
        repository: StateRepository,
        event_store: UiEventStore,
        host: str = "127.0.0.1",
        port: int = 8765,
    ):
        self.config = config
        self.repository = repository
        self.event_store = event_store
        self.host = host
        self.port = port
        self.state_view = HarnessStateView(config, repository, event_store)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        port = self._server.server_address[1] if self._server else self.port
        return f"http://{self.host}:{port}"

    def start(self) -> "HarnessWebServer":
        if self._server:
            return self
        state_view = self.state_view
        translator = DisplayTranslator(self.config)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                try:
                    self._handle_get(state_view)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)

            def do_POST(self) -> None:
                try:
                    self._handle_post(translator)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)

            def log_message(self, format: str, *args) -> None:
                return

            def _handle_get(self, view: HarnessStateView) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send_text(_html(), content_type="text/html; charset=utf-8")
                    return
                if parsed.path == "/api/tasks":
                    self._send_json({"tasks": view.tasks(50), "latest_task_id": view.event_store.latest_task_id})
                    return
                if parsed.path.startswith("/api/tasks/"):
                    task_id = parsed.path.removeprefix("/api/tasks/").strip("/")
                    self._send_json(view.snapshot(task_id or None))
                    return
                if parsed.path == "/api/file":
                    query = parse_qs(parsed.query)
                    path = query.get("path", [""])[0]
                    max_chars = int(query.get("max_chars", ["200000"])[0])
                    self._send_json(view.read_file(path, max_chars=max_chars))
                    return
                self._send_json({"error": "not found"}, status=404)

            def _handle_post(self, translator: DisplayTranslator) -> None:
                parsed = urlparse(self.path)
                if parsed.path != "/api/translate":
                    self._send_json({"error": "not found"}, status=404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(min(length, 250_000))
                payload = json.loads(raw.decode("utf-8") or "{}")
                text = str(payload.get("text") or "")
                self._send_json(translator.translate_to_zh(text))

            def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_text(self, text: str, content_type: str, status: int = 200) -> None:
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="harness-ui", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def file_url(path: str) -> str:
    return f"/api/file?path={quote(path)}"


def _html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Harness Task Console</title>
  <style>
    :root { color-scheme: light; --bg:#f5f6f8; --panel:#ffffff; --panel2:#fbfcfd; --line:#d9dee7; --text:#16202c; --muted:#687386; --accent:#126b63; --accent-soft:#e5f3f0; --warn:#9a5d00; --warn-soft:#fff2d7; --bad:#a23434; --bad-soft:#fde9e9; --good:#247a3d; --good-soft:#e8f5ec; --info:#275b9f; --info-soft:#eaf1fb; }
    * { box-sizing: border-box; }
    body { margin:0; overflow-x:hidden; background:var(--bg); color:var(--text); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    header { min-height:56px; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:10px 18px; border-bottom:1px solid var(--line); background:#fff; position:sticky; top:0; z-index:2; padding-top:max(10px,env(safe-area-inset-top)); }
    h1 { font-size:18px; margin:0; font-weight:700; text-wrap:balance; }
    h2 { font-size:16px; margin:18px 0 10px; font-weight:700; }
    h3 { font-size:14px; margin:0 0 8px; font-weight:700; }
    main { display:grid; grid-template-columns:320px minmax(0,1fr); min-height:calc(100vh - 56px); }
    aside { border-right:1px solid var(--line); background:#fff; padding:14px; overflow:auto; }
    section { padding:16px; overflow:auto; min-width:0; }
    button { border:1px solid var(--line); background:#fff; border-radius:6px; padding:6px 9px; cursor:pointer; color:var(--text); touch-action:manipulation; }
    button:hover { border-color:var(--accent); background:var(--accent-soft); }
    button:focus-visible, a:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
    button:disabled { cursor:not-allowed; color:var(--muted); background:#f2f4f7; }
    .top-actions { display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
    .segmented { display:inline-flex; border:1px solid var(--line); border-radius:7px; overflow:hidden; background:#fff; }
    .segmented button { border:0; border-radius:0; min-width:44px; }
    .segmented button.active { background:var(--accent); color:#fff; }
    .task { width:100%; text-align:left; margin:0 0 8px; display:grid; gap:4px; }
    .task.active { border-color:var(--accent); box-shadow:inset 3px 0 0 var(--accent); background:var(--accent-soft); }
    .task-title { display:flex; justify-content:space-between; gap:8px; min-width:0; }
    .muted { color:var(--muted); }
    .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-variant-numeric:tabular-nums; }
    .grid { display:grid; gap:12px; }
    .overview { grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); margin-bottom:14px; }
    .roles { grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); margin:12px 0; }
    .role-card { width:100%; text-align:left; display:block; cursor:pointer; }
    .role-card:hover { border-color:var(--accent); background:var(--accent-soft); }
    .role-card.selected { border-color:var(--accent); box-shadow:inset 3px 0 0 var(--accent); background:var(--accent-soft); }
    .role-browser { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:12px; margin:12px 0 18px; }
    .role-pane { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; min-width:0; }
    .round-tabs { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 10px; }
    .round-tabs button.active { border-color:var(--accent); background:var(--accent-soft); color:var(--accent); font-weight:700; }
    .delivery-run { border-top:1px solid var(--line); padding-top:10px; margin-top:10px; }
    .split { display:grid; grid-template-columns:minmax(360px,1fr) minmax(360px,1fr); gap:14px; align-items:start; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; min-width:0; }
    .metric { font-size:26px; font-weight:750; letter-spacing:0; line-height:1.1; margin-top:4px; font-variant-numeric:tabular-nums; }
    .status { font-weight:700; }
    .pill { display:inline-flex; align-items:center; min-height:22px; border-radius:999px; padding:2px 8px; font-size:12px; font-weight:700; background:#eef2f6; color:var(--muted); }
    .pill.RUNNING { background:var(--warn-soft); color:var(--warn); }
    .pill.COMPLETED { background:var(--good-soft); color:var(--good); }
    .pill.FAILED,.pill.OUTPUT_INVALID,.pill.TIMEOUT { background:var(--bad-soft); color:var(--bad); }
    .pill.PENDING { background:#eef2f6; color:var(--muted); }
    .COMPLETED { color:var(--good); }
    .FAILED,.OUTPUT_INVALID,.TIMEOUT { color:var(--bad); }
    .RUNNING { color:var(--warn); }
    .workflow { display:flex; gap:8px; overflow-x:auto; padding:2px 0 8px; }
    .step { min-width:140px; border:1px solid var(--line); border-radius:8px; background:#fff; padding:8px; }
    .step.current { border-color:var(--accent); background:var(--accent-soft); }
    .step.done { border-color:#b9d7c2; }
    .step.failed { border-color:#e0b5b5; background:var(--bad-soft); }
    .agent-list { display:grid; gap:8px; }
    .agent-card { border:1px solid var(--line); border-radius:8px; padding:10px; background:var(--panel2); }
    .agent-head { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }
    .files { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
    .file-btn.primary { border-color:var(--accent); color:var(--accent); font-weight:700; }
    .viewer { display:grid; grid-template-columns:minmax(0,1fr); gap:10px; }
    .viewer-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
    .viewer-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }
    .translate-note { margin-top:6px; color:var(--muted); font-size:12px; }
    pre { white-space:pre-wrap; word-break:break-word; margin:0; padding:12px; border:1px solid var(--line); border-radius:8px; background:#111820; color:#e8eef7; max-height:54vh; overflow:auto; tab-size:2; }
    .events { max-height:280px; overflow:auto; }
    .events div { padding:6px 0; border-bottom:1px solid #edf0f3; }
    table { width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
    th,td { border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }
    th { background:#eef2f5; font-weight:650; }
    tr:last-child td { border-bottom:0; }
    .empty { padding:16px; border:1px dashed var(--line); border-radius:8px; color:var(--muted); background:#fff; }
    .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
    @media (max-width: 1040px) { .overview,.split { grid-template-columns:1fr; } }
    @media (max-width: 840px) { main { grid-template-columns:1fr; } aside { border-right:0; border-bottom:1px solid var(--line); max-height:36vh; } }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Harness Task Console</h1>
      <div class="muted" data-i18n="subtitle">看状态、看过程、看产物</div>
    </div>
    <div class="top-actions">
      <div class="segmented" aria-label="Language">
        <button type="button" id="langZh" class="active" onclick="setLanguage('zh')">中</button>
        <button type="button" id="langEn" onclick="setLanguage('en')">EN</button>
      </div>
      <div class="muted mono" id="heartbeat" aria-live="polite">Loading…</div>
    </div>
  </header>
  <main>
    <aside>
      <h2 data-i18n="taskHistory">任务历史</h2>
      <div id="tasks"></div>
    </aside>
    <section aria-live="polite">
      <div id="summary" class="grid overview"></div>
      <h2 data-i18n="workflowProgress">流程进度</h2>
      <div id="workflow" class="workflow"></div>
      <h2 data-i18n="roleStatus">角色状态</h2>
      <div id="roles" class="grid roles"></div>
      <h2 data-i18n="roleDeliveries">角色思考与交付</h2>
      <div class="muted" data-i18n="roleDeliveryHint">选择一个或多个角色，再选择轮次查看该角色的 prompt、stdout、stderr 与交付 md/json。</div>
      <div id="roleBrowser" class="role-browser"></div>
      <div class="split">
        <div>
          <h2 data-i18n="activeAgents">正在执行</h2>
          <div id="activeAgents" class="agent-list"></div>
          <h2 data-i18n="allRuns">全部执行记录</h2>
          <div id="runs"></div>
        </div>
        <div class="viewer">
          <div class="viewer-head">
            <div>
              <h2 data-i18n="visibleOutput">可见输出 & 交付</h2>
              <div id="fileTitle" class="muted">选择 prompt、stdout、stderr 或 artifact。</div>
              <div id="translationNote" class="translate-note"></div>
            </div>
            <div class="viewer-actions">
              <button type="button" onclick="clearViewer()" data-i18n="clear">清空</button>
            </div>
          </div>
          <pre id="fileText"></pre>
          <h2 data-i18n="recentEvents">最近事件</h2>
          <div id="events" class="card events"></div>
        </div>
      </div>
    </section>
  </main>
<script>
let currentTask = new URLSearchParams(location.search).get("task");
let latestData = null;
let uiLanguage = localStorage.getItem("harness-ui-language") || "zh";
let selectedRoles = new Set();
let selectedRoundByRole = {};
let selectedRolesTaskId = null;
let currentFile = null;
let translationSeq = 0;
const translationCache = new Map();
const roleLabels = {planner:"规划者", executor:"执行者", tester:"测试者", reviewer:"审阅者", judge:"裁决者", communicator:"交付者", orchestrator:"编排器"};
const roleLabelsEn = {planner:"Planner", executor:"Executor", tester:"Tester", reviewer:"Reviewer", judge:"Judge", communicator:"Communicator", orchestrator:"Orchestrator"};
const phaseLabels = {
  PLANNING_DRAFT:"规划草案", PLANNING_PEER_REVIEW:"规划互审", PLANNING_REVISION:"规划修订", PLAN_REVIEW:"方案审阅", PLAN_JUDGEMENT:"计划裁决", EXECUTION:"执行实现", PATCH_MERGE:"合并方案",
  TESTING:"测试", TEST_JUDGEMENT:"测试裁决", FIXING:"修复", REVIEWING:"审阅", REVIEW_JUDGEMENT:"审阅裁决",
  REVIEW_FIXING:"审阅修复", REGRESSION_TESTING:"回归测试", FINAL_JUDGEMENT:"最终裁决", DELIVERY:"交付",
  MISC_RESPONSE:"直接回答", COMPLETED:"完成"
};
const phaseLabelsEn = {
  PLANNING_DRAFT:"Planning Draft", PLANNING_PEER_REVIEW:"Planning Peer Review", PLANNING_REVISION:"Planning Revision", PLAN_REVIEW:"Plan Review", PLAN_JUDGEMENT:"Plan Judgement", EXECUTION:"Execution", PATCH_MERGE:"Patch Merge",
  TESTING:"Testing", TEST_JUDGEMENT:"Test Judgement", FIXING:"Fixing", REVIEWING:"Reviewing", REVIEW_JUDGEMENT:"Review Judgement",
  REVIEW_FIXING:"Review Fixing", REGRESSION_TESTING:"Regression Testing", FINAL_JUDGEMENT:"Final Judgement", DELIVERY:"Delivery",
  MISC_RESPONSE:"Direct Response", COMPLETED:"Completed"
};
const i18n = {
  zh: {
    subtitle:"看状态、看过程、看产物", taskHistory:"任务历史", workflowProgress:"流程进度", roleStatus:"角色状态",
    roleDeliveries:"角色思考与交付", roleDeliveryHint:"选择一个或多个角色，再选择轮次查看该角色的 prompt、stdout、stderr 与交付 md/json。",
    activeAgents:"正在执行", allRuns:"全部执行记录", visibleOutput:"可见输出 & 交付", recentEvents:"最近事件", clear:"清空",
    currentTask:"当前任务", running:"正在运行", failures:"失败/重试", artifacts:"交付产物", successPath:"成功路径", taskWorkspace:"工程主目录",
    noTasks:"还没有任务。启动一次 Harness 任务后，这里会显示历史。", noRunning:"当前没有运行中的 agent。任务进行时，这里会优先显示正在执行的角色、日志和产物入口。",
    noRuns:"还没有 agent run。", noEvents:"暂无事件。", selectFile:"选择 prompt、stdout、stderr 或 artifact。",
    noRole:"选择角色卡片后，这里会按角色和轮次显示交付文件。", noRoleOutput:"该角色暂无可查看轮次。",
    autoTranslated:"中文模式：只翻译 prompt、交付物、stdout/stderr 里的说明性文本；文件路径、命令、代码、JSON 和配置保持原文。切换 EN 查看完整原文。",
    translating:"正在翻译说明性文本…",
    translatedByModel:"中文模式：已使用模型翻译说明性文本；文件路径、命令、代码、JSON 和配置保持原文。切换 EN 查看完整原文。",
    translatedFallback:"中文模式：模型翻译不可用，已使用本地词表兜底。切换 EN 查看完整原文。",
    original:"英文模式：显示原文。"
  },
  en: {
    subtitle:"Status, process, and artifacts", taskHistory:"Task History", workflowProgress:"Workflow", roleStatus:"Role Status",
    roleDeliveries:"Role Reasoning and Deliveries", roleDeliveryHint:"Select one or more roles, then choose rounds to inspect prompts, stdout, stderr, and md/json deliveries.",
    activeAgents:"Active Agents", allRuns:"All Runs", visibleOutput:"Visible Output & Delivery", recentEvents:"Recent Events", clear:"Clear",
    currentTask:"Current Task", running:"Running", failures:"Failures / Retries", artifacts:"Artifacts", successPath:"Success Path", taskWorkspace:"Project Root",
    noTasks:"No tasks yet. Start a Harness task and history will appear here.", noRunning:"No running agents. Active role logs and artifacts appear here while a task runs.",
    noRuns:"No agent runs yet.", noEvents:"No events yet.", selectFile:"Select prompt, stdout, stderr, or an artifact.",
    noRole:"Select role cards to show delivery files by role and round.", noRoleOutput:"No viewable rounds for this role yet.",
    autoTranslated:"Chinese mode: content is automatically translated for display; switch to EN for the original.",
    translating:"Translating prose text…",
    translatedByModel:"Chinese mode: prose text was translated by the model; protected content is preserved.",
    translatedFallback:"Chinese mode: model translation is unavailable; local glossary fallback is shown.",
    original:"English mode: showing original content."
  }
};
const workflowOrder = ["PLANNING_DRAFT","PLANNING_PEER_REVIEW","PLANNING_REVISION","PLAN_REVIEW","PLAN_JUDGEMENT","EXECUTION","PATCH_MERGE","TESTING","TEST_JUDGEMENT","FIXING","REVIEWING","REVIEW_JUDGEMENT","REVIEW_FIXING","REGRESSION_TESTING","FINAL_JUDGEMENT","DELIVERY"];
const dateFormat = new Intl.DateTimeFormat(navigator.language || "zh-CN", {hour:"2-digit", minute:"2-digit", second:"2-digit"});
const numberFormat = new Intl.NumberFormat(navigator.language || "zh-CN");

async function getJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

async function postJson(url, payload) {
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

function t(key) {
  return (i18n[uiLanguage] && i18n[uiLanguage][key]) || i18n.zh[key] || key;
}

function roleLabel(role) {
  return uiLanguage === "en" ? (roleLabelsEn[role] || role) : (roleLabels[role] || role);
}

function setLanguage(lang) {
  uiLanguage = lang === "en" ? "en" : "zh";
  localStorage.setItem("harness-ui-language", uiLanguage);
  document.documentElement.lang = uiLanguage === "en" ? "en" : "zh-CN";
  document.getElementById("langZh").classList.toggle("active", uiLanguage === "zh");
  document.getElementById("langEn").classList.toggle("active", uiLanguage === "en");
  document.querySelectorAll("[data-i18n]").forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  if (latestData) renderSnapshot(latestData);
  renderFileText();
}

function persistRoleSelection() {
  if (!selectedRolesTaskId) return;
  localStorage.setItem(selectionStorageKey(selectedRolesTaskId, "roles"), JSON.stringify([...selectedRoles]));
  localStorage.setItem(selectionStorageKey(selectedRolesTaskId, "rounds"), JSON.stringify(selectedRoundByRole));
}

function selectionStorageKey(taskId, suffix) {
  return `harness:${taskId}:selected-${suffix}`;
}

function loadRoleSelection(taskId) {
  if (selectedRolesTaskId === taskId) return;
  selectedRolesTaskId = taskId;
  const storedRoles = localStorage.getItem(selectionStorageKey(taskId, "roles")) || localStorage.getItem("harness-selected-roles") || "[]";
  const storedRounds = localStorage.getItem(selectionStorageKey(taskId, "rounds")) || localStorage.getItem("harness-selected-rounds") || "{}";
  selectedRoles = new Set(JSON.parse(storedRoles));
  selectedRoundByRole = JSON.parse(storedRounds);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

document.addEventListener("click", event => {
  const fileButton = event.target.closest("[data-file-path]");
  if (fileButton) {
    event.stopPropagation();
    openFile(encodeURIComponent(fileButton.dataset.filePath), fileButton.dataset.fileLabel || "artifact");
    return;
  }
  const roundButton = event.target.closest("[data-select-role-round]");
  if (roundButton) {
    event.stopPropagation();
    selectRoleRound(roundButton.dataset.role, roundButton.dataset.roundKey);
    return;
  }
  const roleButton = event.target.closest("[data-toggle-role]");
  if (roleButton) {
    event.stopPropagation();
    toggleRole(roleButton.dataset.toggleRole);
    return;
  }
  const taskButton = event.target.closest("[data-select-task]");
  if (taskButton) {
    selectTask(taskButton.dataset.selectTask);
  }
});

document.addEventListener("keydown", event => {
  if (event.key !== "Enter" && event.key !== " ") return;
  if (event.target.closest("button,a,input,select,textarea")) return;
  const roleCard = event.target.closest(".role-card[data-toggle-role]");
  if (!roleCard) return;
  event.preventDefault();
  toggleRole(roleCard.dataset.toggleRole);
});

function short(s, n=72) {
  s = String(s ?? "").replace(/\s+/g, " ");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

async function refresh() {
  try {
    const taskList = await getJson("/api/tasks");
    const latestTask = (taskList.tasks || []).find(t => t.task_id === taskList.latest_task_id);
    if (taskList.latest_task_id && (!currentTask || (latestTask && latestTask.status === "RUNNING" && currentTask !== taskList.latest_task_id))) {
      currentTask = taskList.latest_task_id;
      history.replaceState(null, "", "?task=" + encodeURIComponent(currentTask));
    }
    if (!currentTask && taskList.tasks.length) currentTask = taskList.tasks[0].task_id;
    renderTasks(taskList.tasks);
    if (currentTask) {
      latestData = await getJson("/api/tasks/" + encodeURIComponent(currentTask));
      renderSnapshot(latestData);
    }
    document.getElementById("heartbeat").textContent = "刷新 " + dateFormat.format(new Date());
  } catch (e) {
    document.getElementById("heartbeat").textContent = "错误：" + e.message;
  }
}

function renderTasks(tasks) {
  const root = document.getElementById("tasks");
  if (!tasks.length) {
    root.innerHTML = `<div class="empty">${esc(t("noTasks"))}</div>`;
    return;
  }
  root.innerHTML = tasks.map((t, i) => `<button class="task ${t.task_id === currentTask ? 'active' : ''}" data-select-task="${esc(t.task_id)}">
    <div class="task-title"><strong>${i + 1}. <span class="mono" translate="no">${esc(t.task_id.slice(0,8))}</span></strong> ${statusPill(t.status)}</div>
    <div class="muted">${esc(labelPhase(t.current_phase || "-"))}</div>
    <div>${esc(short(t.user_prompt, 74))}</div>
  </button>`).join("");
}

function selectTask(taskId) {
  currentTask = taskId;
  history.replaceState(null, "", "?task=" + encodeURIComponent(taskId));
  refresh();
}

function renderSnapshot(data) {
  const task = data.task;
  if (!task) return;
  loadRoleSelection(task.task_id);
  const runs = data.agent_runs || [];
  const running = runs.filter(r => r.status === "RUNNING");
  const failed = runs.filter(r => ["FAILED","OUTPUT_INVALID","TIMEOUT"].includes(r.status));
  const completedArtifacts = runs.reduce((sum, r) => sum + Number(r.artifact_count || 0), 0);
  ensureDefaultSelectedRoles(data);
  document.getElementById("summary").innerHTML = `
    <div class="card">
      <h3>${esc(t("currentTask"))}</h3>
      <div class="mono" translate="no">${esc(task.task_id)}</div>
      <div style="margin-top:8px">${statusPill(task.status)} <span class="muted">${esc(task.workflow_type || "-")} · ${esc(labelPhase(task.current_phase || "-"))}</span></div>
      <div style="margin-top:8px">${esc(task.user_prompt)}</div>
    </div>
    <div class="card"><h3>${esc(t("running"))}</h3><div class="metric">${numberFormat.format(running.length)}</div><div class="muted">${running.length ? (uiLanguage === "en" ? "Roles are working" : "有角色在工作") : (uiLanguage === "en" ? "No active agents" : "当前无运行 agent")}</div></div>
    <div class="card"><h3>${esc(t("failures"))}</h3><div class="metric">${numberFormat.format(failed.length)}</div><div class="muted">${failed.length ? (uiLanguage === "en" ? "Inspect stderr or delivery.md" : "需要检查 stderr 或 delivery.md") : (uiLanguage === "en" ? "No blockers yet" : "暂无阻塞错误")}</div></div>
    <div class="card"><h3>${esc(t("artifacts"))}</h3><div class="metric">${numberFormat.format(completedArtifacts)}</div><div class="muted">${uiLanguage === "en" ? "collected artifacts" : "已收集 artifact"}</div></div>
    <div class="card"><h3>${esc(t("taskWorkspace"))}</h3><div class="mono">${esc(data.task_workspace || "-")}</div></div>
    <div class="card"><h3>${esc(t("successPath"))}</h3><div class="mono">${data.success_path ? esc(data.success_path) : "-"}</div></div>`;
  renderWorkflow(data.phases || [], task.current_phase);
  document.getElementById("roles").innerHTML = Object.values(data.roles || {}).map(r => roleCard(r, runs)).join("");
  renderRoleBrowser(data);
  renderActiveAgents(running);
  renderRuns(data.agent_runs || []);
  renderEvents(data.events || []);
}

function renderWorkflow(phases, currentPhase) {
  const seen = new Map(phases.map(p => [p.phase_type, p]));
  const ordered = workflowOrder.filter(p => seen.has(p) || p === currentPhase || workflowOrder.indexOf(p) <= workflowOrder.indexOf(currentPhase || ""));
  document.getElementById("workflow").innerHTML = ordered.length ? ordered.map(phase => {
    const item = seen.get(phase);
    const status = item?.status || (phase === currentPhase ? "RUNNING" : "PENDING");
    const cls = status === "COMPLETED" ? "done" : status === "FAILED" ? "failed" : phase === currentPhase ? "current" : "";
    return `<div class="step ${cls}"><strong>${esc(labelPhase(phase))}</strong><div>${statusPill(status)}</div><div class="muted">round ${esc(item?.round_id ?? "-")}</div></div>`;
  }).join("") : `<div class="empty">任务启动后会显示阶段流程。</div>`;
}

function roleCard(role, runs) {
  const roleRuns = runs.filter(r => r.role === role.role);
  const latest = roleRuns[roleRuns.length - 1];
  const quick = latest ? [
    fileButton(latest.stdout_path, "stdout", true),
    fileButton(latest.stderr_path, "stderr", false),
    fileButton(latest.diagnostics_path, "diagnostics", false),
    ...preferredArtifacts(latest).map(a => artifactButton(a, true))
  ].join("") : "";
  const selected = selectedRoles.has(role.role);
  return `<div role="button" tabindex="0" class="card role-card ${selected ? "selected" : ""}" data-toggle-role="${esc(role.role)}">
    <div class="agent-head"><h3>${esc(roleLabel(role.role))}</h3>${statusPill(role.status)}</div>
    <div class="muted">${esc(labelPhase(role.phase || "-"))} · ${numberFormat.format(role.agent_count || 0)} agent · ${numberFormat.format(role.artifact_count || 0)} artifact</div>
    <div class="files">${quick || `<span class="muted">${uiLanguage === "en" ? "Waiting for this role." : "等待该角色产出。"}</span>`}</div>
  </div>`;
}

function toggleRole(role) {
  if (selectedRoles.has(role)) selectedRoles.delete(role);
  else selectedRoles.add(role);
  persistRoleSelection();
  if (latestData) {
    document.getElementById("roles").innerHTML = Object.values(latestData.roles || {}).map(r => roleCard(r, latestData.agent_runs || [])).join("");
    renderRoleBrowser(latestData);
  }
}

function ensureDefaultSelectedRoles(data) {
  const rounds = data.role_rounds || {};
  const availableRoles = Object.keys(rounds).filter(role => rounds[role] && rounds[role].length);
  const validSelected = [...selectedRoles].filter(role => availableRoles.includes(role));
  if (validSelected.length !== selectedRoles.size) {
    selectedRoles = new Set(validSelected);
  }
  if (selectedRoles.size) return;
  if (rounds.planner && rounds.planner.length) selectedRoles.add("planner");
  else {
    const first = availableRoles[0];
    if (first) selectedRoles.add(first);
  }
  persistRoleSelection();
}

function renderRoleBrowser(data) {
  const root = document.getElementById("roleBrowser");
  const roundsByRole = data.role_rounds || {};
  const roles = [...selectedRoles].filter(role => roundsByRole[role] && roundsByRole[role].length);
  if (!roles.length) {
    root.innerHTML = `<div class="empty">${esc(t("noRole"))}</div>`;
    return;
  }
  root.innerHTML = roles.map(role => rolePane(role, roundsByRole[role])).join("");
}

function rolePane(role, rounds) {
  if (!rounds.length) {
    return `<div class="role-pane"><h3>${esc(roleLabel(role))}</h3><div class="empty">${esc(t("noRoleOutput"))}</div></div>`;
  }
  const selectedKey = selectedRoundByRole[role] || roundKey(rounds[rounds.length - 1]);
  const selected = rounds.find(item => roundKey(item) === selectedKey) || rounds[rounds.length - 1];
  selectedRoundByRole[role] = roundKey(selected);
  persistRoleSelection();
  return `<div class="role-pane">
    <div class="agent-head"><h3>${esc(roleLabel(role))}</h3><button type="button" data-toggle-role="${esc(role)}">${uiLanguage === "en" ? "Hide" : "隐藏"}</button></div>
    <div class="round-tabs">${rounds.map(item => {
      const key = roundKey(item);
      const active = key === selectedRoundByRole[role];
      return `<button type="button" class="${active ? "active" : ""}" data-select-role-round="1" data-role="${esc(role)}" data-round-key="${esc(key)}">round ${esc(item.round_id)} · ${esc(labelPhase(item.phase_type))}</button>`;
    }).join("")}</div>
    ${selected.runs.map(run => roleRunDelivery(run)).join("")}
  </div>`;
}

function roleRunDelivery(run) {
  const deliveryArtifacts = (run.artifacts || []).filter(a => isDeliveryArtifact(a.artifact_type));
  const otherArtifacts = (run.artifacts || []).filter(a => !isDeliveryArtifact(a.artifact_type));
  return `<div class="delivery-run">
    <div class="agent-head">
      <strong><span translate="no">${esc(run.agent_id)}</span></strong>
      ${statusPill(run.status)}
    </div>
    <div class="muted">${esc(labelPhase(run.phase_type || "-"))} · try ${Number(run.retry_count) + 1}</div>
    <div class="files">
      ${fileButton(run.prompt_path, "prompt", false)}
      ${fileButton(run.stdout_path, "stdout", true)}
      ${fileButton(run.stderr_path, "stderr", false)}
      ${fileButton(run.diagnostics_path, "diagnostics", false)}
      ${deliveryArtifacts.map(a => artifactButton(a, true)).join("")}
      ${otherArtifacts.map(a => artifactButton(a, false)).join("")}
    </div>
  </div>`;
}

function selectRoleRound(role, key) {
  selectedRoundByRole[role] = key;
  persistRoleSelection();
  if (latestData) renderRoleBrowser(latestData);
}

function roundKey(item) {
  return `${item.round_id}:${item.phase_type}`;
}

function isDeliveryArtifact(name) {
  return ["delivery.md","final_delivery.md","usage_guide.md","response.md","plan.md","decision_summary.md","review_report.md","test_report.md","bug_report.md","self_check.md"].includes(name);
}

function renderActiveAgents(running) {
  const root = document.getElementById("activeAgents");
  if (!running.length) {
    root.innerHTML = `<div class="empty">${esc(t("noRunning"))}</div>`;
    return;
  }
  root.innerHTML = running.map(run => agentCard(run, true)).join("");
}

function renderRuns(runs) {
  const root = document.getElementById("runs");
  if (!runs.length) {
    root.innerHTML = `<div class="empty">${esc(t("noRuns"))}</div>`;
    return;
  }
  root.innerHTML = runs.slice().reverse().map(run => agentCard(run, false)).join("");
}

function agentCard(run, compact) {
  return `<div class="agent-card">
    <div class="agent-head">
      <div>
        <strong>${esc(roleLabel(run.role))} / <span translate="no">${esc(run.agent_id)}</span></strong>
        <div class="muted">${esc(labelPhase(run.phase_type || "-"))} · round ${esc(run.phase_round_id ?? "-")} · try ${Number(run.retry_count) + 1}</div>
      </div>
      ${statusPill(run.status)}
    </div>
    <div class="files">
      ${fileButton(run.prompt_path, "prompt", false)}
      ${fileButton(run.stdout_path, "stdout", true)}
      ${fileButton(run.stderr_path, "stderr", false)}
      ${(run.artifacts || []).map(a => artifactButton(a, false)).join("")}
    </div>
    ${compact ? "" : `<div class="muted mono" style="margin-top:8px" translate="no">${esc(run.log_dir)}</div>`}
  </div>`;
}

function fileButton(info, label, primary) {
  if (!info || !info.exists) return `<button type="button" disabled>${esc(label)}</button>`;
  const size = info.size === null || info.size === undefined ? "" : ` ${formatBytes(info.size)}`;
  const display = fileLabel(label);
  return `<button type="button" class="file-btn ${primary ? "primary" : ""}" data-file-path="${esc(info.path)}" data-file-label="${esc(display)}">${esc(display)}${size}</button>`;
}

function artifactButton(a, primary) {
  if (!a.exists) return "";
  return `<button type="button" class="file-btn ${primary ? "primary" : ""}" data-file-path="${esc(a.path)}" data-file-label="${esc(a.artifact_type)}">${esc(short(a.artifact_type, 28))}</button>`;
}

function preferredArtifacts(run) {
  const priority = ["final_delivery.md","usage_guide.md","response.md","merged_patch.diff","merge_report.md","test_report.md","bug_report.md","review_report.md","decision_summary.md","plan.md","todo_breakdown.md","delivery.md"];
  return (run.artifacts || []).slice().sort((a, b) => priorityIndex(a.artifact_type, priority) - priorityIndex(b.artifact_type, priority)).slice(0, 3);
}

function priorityIndex(name, priority) {
  const index = priority.indexOf(name);
  return index === -1 ? 999 : index;
}

function fileLabel(label) {
  if (uiLanguage === "en") return label === "prompt" ? "full prompt" : label;
  if (label === "prompt") return "完整提示词";
  if (label === "stdout") return "标准输出";
  if (label === "stderr") return "错误输出";
  if (label === "diagnostics") return "请求诊断";
  return label;
}

async function openFile(encodedPath, label) {
  const data = await getJson("/api/file?path=" + encodedPath + "&max_chars=200000");
  currentFile = {label, ...data};
  renderFileText();
}

function clearViewer() {
  currentFile = null;
  document.getElementById("fileTitle").textContent = t("selectFile");
  document.getElementById("fileText").textContent = "";
  document.getElementById("translationNote").textContent = "";
}

function renderFileText() {
  if (!currentFile) {
    document.getElementById("fileTitle").textContent = t("selectFile");
    document.getElementById("translationNote").textContent = "";
    return;
  }
  const suffix = currentFile.truncated_from_start ? (uiLanguage === "en" ? " (showing tail)" : "（显示尾部）") : "";
  document.getElementById("fileTitle").textContent = currentFile.label + " · " + currentFile.path + suffix;
  const source = currentFile.text || "";
  if (uiLanguage !== "zh") {
    document.getElementById("fileText").textContent = source;
    document.getElementById("translationNote").textContent = t("original");
    return;
  }
  const cacheKey = currentFile.path + ":" + currentFile.size + ":" + source.length;
  const cached = translationCache.get(cacheKey);
  if (cached) {
    document.getElementById("fileText").textContent = cached.text;
    document.getElementById("translationNote").textContent = cached.mode === "model" ? t("translatedByModel") : t("translatedFallback");
    return;
  }
  const fallback = translateMarkdownToChinese(source);
  document.getElementById("fileText").textContent = fallback;
  document.getElementById("translationNote").textContent = t("translating");
  const seq = ++translationSeq;
  const path = currentFile.path;
  postJson("/api/translate", {text: source, path}).then(data => {
    if (!currentFile || currentFile.path !== path || uiLanguage !== "zh" || seq !== translationSeq) return;
    const translated = data.text || fallback;
    const mode = data.mode || "fallback";
    translationCache.set(cacheKey, {text: translated, mode});
    document.getElementById("fileText").textContent = translated;
    document.getElementById("translationNote").textContent = mode === "model" ? t("translatedByModel") : t("translatedFallback");
  }).catch(() => {
    if (!currentFile || currentFile.path !== path || uiLanguage !== "zh" || seq !== translationSeq) return;
    translationCache.set(cacheKey, {text: fallback, mode: "fallback"});
    document.getElementById("fileText").textContent = fallback;
    document.getElementById("translationNote").textContent = t("translatedFallback");
  });
}

function translateMarkdownToChinese(text) {
  if (!text) return text;
  let inFence = false;
  return text.split("\n").map(line => {
    if (/^\s*```/.test(line)) {
      inFence = !inFence;
      return line;
    }
    if (inFence || shouldPreserveLine(line)) return line;
    return translateProseLine(line);
  }).join("\n");
}

function shouldPreserveLine(line) {
  const trimmed = line.trim();
  if (!trimmed) return true;
  if (hasMostlyChinese(trimmed)) return true;
  if (/^(diff --git|index |--- |\+\+\+ |@@ |[+-]{3,})/.test(trimmed)) return true;
  if (/^[+-]\s/.test(trimmed) && /[`$./\\]|^\+\s*(import|from|def|class|const|let|var|function)\b/.test(trimmed)) return true;
  if (/^(curl|python3?|pip|npm|pnpm|yarn|bun|uv|pytest|git|docker|make|cargo|go|node|claude|codex|source|cd|mkdir|cp|mv|rm|cat|sed|rg|grep|ls|open)\b/.test(trimmed)) return true;
  if (/^\$ /.test(trimmed)) return true;
  if (/^(https?:\/\/|file:\/\/)/.test(trimmed)) return true;
  if (/^(\/|~\/|\.\.?\/)[^\s]*$/.test(trimmed)) return true;
  if (/^[A-Za-z]:[\\/]/.test(trimmed)) return true;
  if (/^[-*]\s+(`[^`]+`|\/|~\/|\.\.?\/|https?:\/\/)/.test(trimmed)) return true;
  if (/^\s*[{[\]}],?\s*$/.test(line)) return true;
  if (/^\s*"[^"]+"\s*:\s*("[^"]*"|\d+|true|false|null|[{[]),?\s*$/.test(line)) return true;
  if (/^\s*[A-Z0-9_]+\s*=/.test(line)) return true;
  if (/^\s*[-*]\s+[A-Za-z0-9_.\/~-]+\.(md|py|js|ts|tsx|json|yaml|yml|txt|log|diff|patch|html|css|sh)\b/.test(line)) return true;
  return false;
}

function translateProseLine(line) {
  const placeholders = [];
  let protectedLine = line.replace(/`[^`]*`|https?:\/\/\S+|(?:\/|~\/|\.\.?\/)[^\s),;]+|[A-Za-z0-9_.-]+\.(?:md|py|js|ts|tsx|json|yaml|yml|txt|log|diff|patch|html|css|sh)\b/g, token => {
    const marker = `__KEEP_${placeholders.length}__`;
    placeholders.push(token);
    return marker;
  });
  protectedLine = applyTranslationGlossary(protectedLine);
  return protectedLine.replace(/__KEEP_(\d+)__/g, (_, index) => placeholders[Number(index)] ?? "");
}

function applyTranslationGlossary(text) {
  let out = text;
  const replacements = [
    [/^(\s*#{1,6}\s*)Success Path\b/g, "$1成功路径"],
    [/^(\s*#{1,6}\s*)Delivery Artifact Manifest\b/g, "$1交付产物清单"],
    [/^(\s*#{1,6}\s*)Final Delivery\b/g, "$1最终交付"],
    [/^(\s*#{1,6}\s*)Usage Guide\b/g, "$1使用指南"],
    [/^(\s*#{1,6}\s*)Published Files\b/g, "$1已发布文件"],
    [/^(\s*#{1,6}\s*)Supporting Artifacts\b/g, "$1支撑产物"],
    [/^(\s*#{1,6}\s*)Materialized Source Files\b/g, "$1已物化源码文件"],
    [/^(\s*#{1,6}\s*)Status\b/g, "$1状态"],
    [/^(\s*#{1,6}\s*)Summary\b/g, "$1摘要"],
    [/^(\s*#{1,6}\s*)Validation\b/g, "$1验证"],
    [/^(\s*#{1,6}\s*)Risks\b/g, "$1风险"],
    [/^(\s*#{1,6}\s*)Next Steps\b/g, "$1下一步"],
    [/^(\s*#{1,6}\s*)Prerequisites\b/g, "$1前置条件"],
    [/^(\s*[-*]\s*)Use the collected artifacts for this task\./gi, "$1使用为此任务收集的产物。"],
    [/^(\s*[-*]\s*)Confirm every role delivery reports status success\./gi, "$1确认每个角色的交付都报告状态为成功。"],
    [/^(\s*[-*]\s*)Confirm final judge approval exists\./gi, "$1确认存在最终 judge 批准。"],
    [/^(\s*\d+\.\s*)Open final delivery for the outcome summary\./gi, "$1打开最终交付查看结果摘要。"],
    [/^(\s*\d+\.\s*)Review implementation, test, review, and judge artifacts for supporting evidence\./gi, "$1查看实现、测试、审阅和裁决产物作为支撑证据。"],
    [/^(\s*\d+\.\s*)Apply or inspect the patch artifact produced by the executor\./gi, "$1应用或检查 executor 产出的补丁产物。"],
    [/\bYou are the ([a-z]+) role for a Harness-managed coding task\./gi, "你是 Harness 管理的编码任务中的 $1 角色。"],
    [/\bProduce ([^.]+) only\./gi, "只产出 $1。"],
    [/\bDo not modify source files\./gi, "不要修改源码文件。"],
    [/\bDo not create implementation changes\./gi, "不要创建实现变更。"],
    [/\bDo not invent details not supported by artifacts or update global Harness state\./gi, "不要编造产物不支持的细节，也不要更新全局 Harness 状态。"],
    [/\bSummarize the accepted outcome, final status, produced artifacts, validation evidence, known risks, and recommended next steps/gi, "总结已接受结果、最终状态、已产生产物、验证证据、已知风险和建议下一步"],
    [/\bExplain how to use the delivered result/gi, "说明如何使用交付结果"],
    [/\bincluding prerequisites, setup, run commands, configuration, verification, common failure modes, and artifact locations/gi, "包括前置条件、设置、运行命令、配置、验证、常见失败模式和产物位置"],
    [/\bTask\b/g, "任务"],
    [/\bRole\b/g, "角色"],
    [/\bPhase\b/g, "阶段"],
    [/\bAgent\b/g, "Agent"],
    [/\bAttempt\b/g, "尝试"],
    [/\bRound\b/g, "轮次"],
    [/\bUser request\b/gi, "用户请求"],
    [/\bRequired outputs\b/gi, "必需输出"],
    [/\bInput artifacts\b/gi, "输入产物"],
    [/\bOutput directory\b/gi, "输出目录"],
    [/\bWorkspace directory\b/gi, "工作区目录"],
    [/\bRepository directory\b/gi, "仓库目录"],
    [/\bImplementation\b/gi, "实现"],
    [/\bTesting\b/gi, "测试"],
    [/\bReview\b/gi, "审阅"],
    [/\bJudge\b/gi, "裁决"],
    [/\bCommunicator\b/gi, "交付者"],
    [/\bPlanner\b/gi, "规划者"],
    [/\bExecutor\b/gi, "执行者"],
    [/\bTester\b/gi, "测试者"],
    [/\bReviewer\b/gi, "审阅者"],
    [/\baccepted outcome\b/gi, "已接受结果"],
    [/\bfinal status\b/gi, "最终状态"],
    [/\bproduced artifacts\b/gi, "已产生产物"],
    [/\bvalidation evidence\b/gi, "验证证据"],
    [/\bknown risks\b/gi, "已知风险"],
    [/\brecommended next steps\b/gi, "建议下一步"],
    [/\bprerequisites\b/gi, "前置条件"],
    [/\bsetup\b/gi, "设置"],
    [/\brun commands\b/gi, "运行命令"],
    [/\bconfiguration\b/gi, "配置"],
    [/\bverification\b/gi, "验证"],
    [/\bcommon failure modes\b/gi, "常见失败模式"],
    [/\bartifact locations\b/gi, "产物位置"],
    [/\bcompleted\b/gi, "已完成"],
    [/\bcomplete\b/gi, "完整"],
    [/\bsuccess\b/gi, "成功"],
    [/\bfailed\b/gi, "失败"],
    [/\bpartial\b/gi, "部分完成"],
    [/\bnone\b/gi, "无"],
  ];
  for (const [pattern, value] of replacements) out = out.replace(pattern, value);
  out = out.replace(/\b([a-z_]+):/gi, (match, key) => {
    const labels = {
      status: "状态",
      role: "角色",
      phase: "阶段",
      agent_id: "Agent ID",
      task_id: "任务 ID",
      summary: "摘要",
      known_risks: "已知风险",
      success_path: "成功路径",
      final_delivery: "最终交付",
      usage_guide: "使用指南",
      artifacts_manifest: "产物清单",
      source_final_delivery: "源最终交付",
      published_final_delivery: "已发布最终交付",
    };
    return labels[key.toLowerCase()] ? `${labels[key.toLowerCase()]}:` : match;
  });
  return out;
}

function hasMostlyChinese(text) {
  const sample = text.slice(0, 4000);
  const chinese = (sample.match(/[\u4e00-\u9fff]/g) || []).length;
  const letters = (sample.match(/[A-Za-z]/g) || []).length;
  return chinese > 0 && chinese >= letters * 0.25;
}

function renderEvents(events) {
  const root = document.getElementById("events");
  if (!events.length) {
    root.innerHTML = `<div class="muted">${esc(t("noEvents"))}</div>`;
    return;
  }
  root.innerHTML = events.slice(-80).reverse().map(e => `<div>
    <strong>${esc(e.event_type)}</strong> ${esc(labelPhase(e.phase || ""))} ${esc(e.role ? roleLabel(e.role) : "")} ${esc(e.agent_id || "")}
    <span class="${esc(e.status)}">${esc(e.status || "")}</span>
    <div class="muted">${esc(e.message || "")}</div>
  </div>`).join("");
}

function statusPill(status) {
  const text = status || "PENDING";
  return `<span class="pill ${esc(text)}">${esc(text)}</span>`;
}

function labelPhase(phase) {
  return (uiLanguage === "en" ? phaseLabelsEn[phase] : phaseLabels[phase]) || phase || "-";
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "";
  if (bytes < 1024) return `${numberFormat.format(bytes)} B`;
  if (bytes < 1024 * 1024) return `${numberFormat.format(Math.round(bytes / 1024))} KB`;
  return `${numberFormat.format((bytes / 1024 / 1024).toFixed(1))} MB`;
}

setLanguage(uiLanguage);
clearViewer();
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""
