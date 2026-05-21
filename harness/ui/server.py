from __future__ import annotations

import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

from harness.state.repository import StateRepository
from harness.ui.api import (
    api_error_payload,
    build_api_handler,
    parse_json_object_body,
    require_string_field,
    validate_runtime_config_payload,
)
from harness.ui.html import render_html as _html
from harness.ui.state_view import HarnessStateView, UiEventStore
from harness.ui.translation import DisplayTranslator


CLIENT_DISCONNECT_EXCEPTIONS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError)


class QuietClientDisconnectHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: tuple[str, int] | object) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_DISCONNECT_EXCEPTIONS):
            return
        super().handle_error(request, client_address)


class HarnessWebServer:
    def __init__(
        self,
        config: dict[str, Any],
        repository: StateRepository,
        event_store: UiEventStore,
        host: str = "127.0.0.1",
        port: int = 8765,
        config_path: str | Path | None = None,
    ):
        self.config = config
        self.repository = repository
        self.event_store = event_store
        self.host = host
        self.port = port
        self.state_view = HarnessStateView(config, repository, event_store, config_path)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        port = self._server.server_address[1] if self._server else self.port
        return f"http://{self.host}:{port}"

    def start(self) -> "HarnessWebServer":
        if self._server:
            return self
        translator = DisplayTranslator(self.config)
        Handler = build_api_handler(self.state_view, translator, _html)

        self._server = QuietClientDisconnectHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="harness-ui", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def file_url(path: str) -> str:
    return f"/api/file?path={quote(path)}"
