"""Tests for websocket logs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from server.app import create_app
from tests.server.test_rpc import StubAdapter, StubRuntime


def test_log_websocket_streams_append_events_for_selected_file(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    log_file.write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\n",
        encoding="utf-8",
    )
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/logs?file=2026-05-11") as websocket:
            wait_for_log_subscriber(app, "2026-05-11")
            assert app.state.event_bus.subscriber_count == 0

            log_file.write_text(
                "\n".join(
                    [
                        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready",
                        "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            event = websocket.receive_json()
            websocket.close()

        wait_for_log_viewer_idle(app)

    assert event == {
        "type": "append",
        "file": "2026-05-11",
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:01",
                "level": "error",
                "logger_name": "vbot.server.app",
                "message": "Failed",
                "continuation": "",
                "raw": "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed",
            }
        ],
    }


def test_log_websocket_replays_handoff_entries_appended_after_log_read(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    log_file.write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\n",
        encoding="utf-8",
    )
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        read_response = client.post(
            "/api/rpc",
            json={"method": "log.read", "params": {"file": "2026-05-11"}},
        )
        cursor = read_response.json()["result"]["cursor"]

        log_file.write_text(
            "\n".join(
                [
                    "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready",
                    "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with client.websocket_connect(f"/ws/logs?file=2026-05-11&cursor={cursor}") as websocket:
            event = websocket.receive_json()
            websocket.close()

        wait_for_log_viewer_idle(app)

    assert event == {
        "type": "append",
        "file": "2026-05-11",
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:01",
                "level": "error",
                "logger_name": "vbot.server.app",
                "message": "Failed",
                "continuation": "",
                "raw": "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed",
            }
        ],
    }


def test_log_websocket_streams_reset_events_when_file_is_truncated(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    log_file.write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready",
                "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed",
                "",
            ]
        ),
        encoding="utf-8",
    )
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/logs?file=2026-05-11") as websocket:
            wait_for_log_subscriber(app, "2026-05-11")

            log_file.write_text(
                "2026-05-11 09:00:02 [WARN] vbot.server.app - Reset\n",
                encoding="utf-8",
            )

            event = websocket.receive_json()
            websocket.close()

        wait_for_log_viewer_idle(app)

    assert event == {
        "type": "reset",
        "file": "2026-05-11",
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:02",
                "level": "warn",
                "logger_name": "vbot.server.app",
                "message": "Reset",
                "continuation": "",
                "raw": "2026-05-11 09:00:02 [WARN] vbot.server.app - Reset",
            }
        ],
    }


def test_log_websocket_filters_routine_websocket_noise_from_append_events(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    log_file.write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\n",
        encoding="utf-8",
    )
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/logs?file=2026-05-11") as websocket:
            wait_for_log_subscriber(app, "2026-05-11")

            log_file.write_text(
                "\n".join(
                    [
                        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready",
                        "2026-05-11 09:00:01 [INFO] vbot.server.uvicorn - "
                        '127.0.0.1:55090 - "WebSocket /ws" [accepted]',
                        "2026-05-11 09:00:02 [INFO] vbot.server.uvicorn - connection open",
                        "2026-05-11 09:00:03 [WARN] vbot.server.uvicorn - keepalive ping timeout",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            event = websocket.receive_json()
            websocket.close()

        wait_for_log_viewer_idle(app)

    assert event == {
        "type": "append",
        "file": "2026-05-11",
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:03",
                "level": "warn",
                "logger_name": "vbot.server.uvicorn",
                "message": "keepalive ping timeout",
                "continuation": "",
                "raw": "2026-05-11 09:00:03 [WARN] vbot.server.uvicorn - keepalive ping timeout",
            }
        ],
    }


def test_log_websocket_filters_routine_websocket_noise_from_reset_events(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    log_file.write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready",
                "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed",
                "",
            ]
        ),
        encoding="utf-8",
    )
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/logs?file=2026-05-11") as websocket:
            wait_for_log_subscriber(app, "2026-05-11")

            log_file.write_text(
                "\n".join(
                    [
                        "2026-05-11 09:00:02 [INFO] vbot.server.uvicorn - "
                        '127.0.0.1:60756 - "WebSocket /ws/logs?cursor=abc" [accepted]',
                        "2026-05-11 09:00:03 [INFO] vbot.server.uvicorn - connection closed",
                        "2026-05-11 09:00:04 [ERROR] vbot.server.uvicorn - "
                        "opening handshake failed",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            event = websocket.receive_json()
            websocket.close()

        wait_for_log_viewer_idle(app)

    assert event == {
        "type": "reset",
        "file": "2026-05-11",
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:04",
                "level": "error",
                "logger_name": "vbot.server.uvicorn",
                "message": "opening handshake failed",
                "continuation": "",
                "raw": "2026-05-11 09:00:04 [ERROR] vbot.server.uvicorn - opening handshake failed",
            }
        ],
    }


def test_log_websocket_disconnect_releases_watcher_resources(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-11").write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\n",
        encoding="utf-8",
    )
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws/logs?file=2026-05-11") as websocket:
            wait_for_log_subscriber(app, "2026-05-11")
            websocket.close()

        wait_for_log_viewer_idle(app)

    assert app.state.log_viewer.watcher_count == 0


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
