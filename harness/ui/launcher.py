from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.orchestrator import Orchestrator
from harness.ui.server import HarnessWebServer, UiEventStore


def start_ui_server(
    config: dict[str, Any],
    orchestrator: Orchestrator,
    ui_store: UiEventStore,
    port: int | None = None,
    config_path: str | Path | None = None,
) -> HarnessWebServer:
    visualization = config.get("visualization", {})
    host = str(visualization.get("host", "127.0.0.1"))
    requested_port = int(port if port is not None else visualization.get("port", 8765))
    server = HarnessWebServer(
        config,
        orchestrator.repository,
        ui_store,
        host=host,
        port=requested_port,
        config_path=config_path,
    )
    try:
        server.start()
    except OSError:
        if port is not None:
            raise
        server = HarnessWebServer(
            config,
            orchestrator.repository,
            ui_store,
            host=host,
            port=0,
            config_path=config_path,
        ).start()
        print(f"[ui] Port {requested_port} unavailable; using {server.url}")
    print(f"[ui] OpenOrchestra Execution Viewer: {server.url}")
    return server
