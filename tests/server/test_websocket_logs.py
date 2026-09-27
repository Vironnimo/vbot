"""Tests for the /ws/logs live log tail."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from server.app import create_app
from tests.server.rpc_test_support import StubAdapter, StubRuntime

_READY = "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready"
_FAILED = "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed"
_RESET = "2026-05-11 09:00:02 [WARN] vbot.server.app - Reset"
_FAILED_ENTRY = {
    "timestamp": "2026-05-11 09:00:01",
    "level": "error",
    "logger_name": "vbot.server.app",
    "message": "Failed",
    "continuation": "",
    "raw": _FAILED,
}


def _log_app(tmp_path: Path) -> tuple[Any, Path]:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    log_file.write_text(f"{_READY}\n", encoding="utf-8")
    return create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter()))), log_file


def test_log_websocket_streams_appends_and_resets_for_the_selected_file(tmp_path: Path) -> None:
    app, log_file = _log_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/logs?file=2026-05-11") as websocket:
            wait_for_log_subscriber(app, "2026-05-11")
            # The log tail is its own stream, not an event-bus subscription.
            assert app.state.event_bus.subscriber_count == 0
            # A stray client frame must not end server-push delivery.
            websocket.send_text("keepalive")

            log_file.write_text(f"{_READY}\n{_FAILED}\n", encoding="utf-8")
            append = websocket.receive_json()
            log_file.write_text(f"{_RESET}\n", encoding="utf-8")
            reset = websocket.receive_json()
            websocket.close()

        wait_for_log_viewer_idle(app)

    assert append == {"type": "append", "file": "2026-05-11", "entries": [_FAILED_ENTRY]}
    assert reset == {
        "type": "reset",
        "file": "2026-05-11",
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:02",
                "level": "warn",
                "logger_name": "vbot.server.app",
                "message": "Reset",
                "continuation": "",
                "raw": _RESET,
            }
        ],
    }
    # Disconnecting releases the file watcher.
    assert app.state.log_viewer.watcher_count == 0


def test_log_websocket_replays_handoff_entries_appended_after_log_read(tmp_path: Path) -> None:
    app, log_file = _log_app(tmp_path)

    with TestClient(app) as client:
        read_response = client.post(
            "/api/rpc",
            json={"method": "log.read", "params": {"file": "2026-05-11"}},
        )
        cursor = read_response.json()["result"]["cursor"]

        log_file.write_text(f"{_READY}\n{_FAILED}\n", encoding="utf-8")

        with client.websocket_connect(f"/ws/logs?file=2026-05-11&cursor={cursor}") as websocket:
            event = websocket.receive_json()
            websocket.close()

        wait_for_log_viewer_idle(app)

    assert event == {"type": "append", "file": "2026-05-11", "entries": [_FAILED_ENTRY]}


def wait_for_log_subscriber(app: Any, file_name: str, timeout_seconds: float = 2.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if (
            app.state.log_viewer.watcher_count == 1
            and app.state.log_viewer.subscriber_count(file_name) == 1
        ):
            return
        time.sleep(0.01)

    raise AssertionError(f"timed out waiting for log subscriber: {file_name}")


def wait_for_log_viewer_idle(app: Any, timeout_seconds: float = 2.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if app.state.log_viewer.watcher_count == 0:
            return
        time.sleep(0.01)

    raise AssertionError("timed out waiting for log viewer cleanup")
