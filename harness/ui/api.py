from __future__ import annotations

import json
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from harness.ui.state_view import HarnessStateView, UiEventStore
    from harness.ui.translation import DisplayTranslator


def api_error_payload(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def parse_json_object_body(raw: bytes, endpoint: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{endpoint} request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{endpoint} request body must be a JSON object")
    return payload


def validate_runtime_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {"agent_backend", "roles", "runtime", "persist"}
    unknown_keys = sorted(set(payload) - allowed_keys)
    if unknown_keys:
        raise ValueError(f"/api/config request contains unsupported field(s): {', '.join(unknown_keys)}")
    if "persist" in payload and not isinstance(payload["persist"], bool):
        raise ValueError("/api/config persist must be a boolean")
    return payload


def require_string_field(payload: dict[str, Any], field: str, endpoint: str, max_chars: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{endpoint} {field} must be a string")
    if len(value) > max_chars:
        raise ValueError(f"{endpoint} {field} exceeds {max_chars} characters")
    return value


def build_api_handler(
    state_view: HarnessStateView,
    translator: DisplayTranslator,
    html_renderer: Callable[[], str],
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                self._handle_get(state_view, html_renderer)
            except PermissionError as exc:
                self._send_json(api_error_payload("forbidden_path", str(exc)), status=403)
            except FileNotFoundError as exc:
                self._send_json(api_error_payload("file_not_found", str(exc)), status=404)
            except ValueError as exc:
                self._send_json(api_error_payload("bad_request", str(exc)), status=400)
            except Exception as exc:
                self._send_json(api_error_payload("internal_error", str(exc)), status=500)

        def do_POST(self) -> None:
            try:
                self._handle_post(translator, state_view)
            except PermissionError as exc:
                self._send_json(api_error_payload("forbidden_path", str(exc)), status=403)
            except FileNotFoundError as exc:
                self._send_json(api_error_payload("file_not_found", str(exc)), status=404)
            except ValueError as exc:
                self._send_json(api_error_payload("bad_request", str(exc)), status=400)
            except Exception as exc:
                self._send_json(api_error_payload("internal_error", str(exc)), status=500)

        def log_message(self, format: str, *args) -> None:
            return

        def _handle_get(self, view: HarnessStateView, render_html: Callable[[], str]) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_text(render_html(), content_type="text/html; charset=utf-8")
                return
            if parsed.path == "/api/events":
                query = parse_qs(parsed.query)
                task_id = query.get("task", [None])[0] or None
                last_event_id = int(query.get("last_id", ["0"])[0] or "0")
                self._send_event_stream(view.event_store, task_id, last_event_id)
                return
            if parsed.path == "/api/tasks":
                self._send_json({"tasks": view.tasks(50), "latest_task_id": view.event_store.latest_task_id})
                return
            if parsed.path == "/api/config":
                self._send_json(view.get_runtime_config())
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
            self._send_json(api_error_payload("not_found", "not found"), status=404)

        def _handle_post(self, translator: DisplayTranslator, view: HarnessStateView) -> None:
            parsed = urlparse(self.path)

            if parsed.path == "/api/config":
                payload = validate_runtime_config_payload(self._read_json_body("/api/config"))
                if view.has_active_task():
                    self._send_json(
                        api_error_payload(
                            "runtime_config_locked",
                            "系统中有正在运行的任务，运行配置已锁定 / Runtime config is locked while tasks are active",
                        ),
                        status=400,
                    )
                    return

                self._send_json({"status": "ok", "config": view.update_runtime_config(payload)})
                return

            if parsed.path != "/api/translate":
                self._send_json(api_error_payload("not_found", "not found"), status=404)
                return
            payload = self._read_json_body("/api/translate")
            text = require_string_field(payload, "text", "/api/translate", 200_000)
            self._send_json(translator.translate_to_zh(text))

        def _read_json_body(self, endpoint: str) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(min(length, 250_000))
            return parse_json_object_body(raw, endpoint)

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

        def _send_event_stream(self, event_store: UiEventStore, task_id: str | None, last_event_id: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            deadline = time.time() + 300
            current_id = last_event_id
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while time.time() < deadline:
                    events = event_store.events_since(current_id, task_id)
                    if not events:
                        event_store.wait_for_events(current_id, timeout_seconds=15)
                        events = event_store.events_since(current_id, task_id)
                    if not events:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        continue
                    for event in events:
                        current_id = max(current_id, int(event.get("id") or 0))
                        payload = json.dumps(event, ensure_ascii=False, default=str)
                        self.wfile.write(f"id: {current_id}\nevent: progress\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return

    return Handler
