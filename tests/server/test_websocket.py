"""Tests for WebSocket server event push."""

from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import aclosing
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from server.app import _parse_after_sequence, create_app
from server.delegates import RUN_DELTA_EVENT_TYPES, RUN_OUTPUT_EVENT_TYPES, SERVER_EVENT_TYPES
from server.events import ALLOWED_SERVER_EVENT_TYPES, APP_ERROR_EVENT, ServerEventBus
from tests.server.test_rpc import StubAdapter, StubRuntime


def _close_log_viewer_now(app: Any) -> None:
    with contextlib.suppress(Exception):
        asyncio.run(app.state.log_viewer.aclose())


def test_websocket_receives_run_lifecycle_events_without_provider_metadata(tmp_path: Path) -> None:
    adapter = StubAdapter(
        stream_deltas=[
            {"type": "reasoning_delta", "text": "Readable thinking"},
            {"type": "reasoning_meta", "reasoning_meta": {"secret": "opaque"}},
            {
                "type": "content_delta",
                "text": "Hello",
            },
        ]
    )
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, adapter)))

    with TestClient(app) as client:
        client.post(
            "/api/rpc",
            json={
                "method": "session.create",
                "params": {"agent_id": "coder", "session_id": "session-one"},
            },
        )
        with client.websocket_connect("/ws") as websocket:
            response = client.post(
                "/api/rpc",
                json={
                    "method": "chat.stream",
                    "params": {"agent_id": "coder", "session_id": "session-one", "content": "Hi"},
                },
            )
            run_id = response.json()["result"]["run_id"]
            events = [websocket.receive_json() for _ in range(5)]

    assert [event["type"] for event in events] == [
        "run_started",
        "run_output",
        "run_output",
        "run_output",
        "run_completed",
    ]
    assert all(event["payload"]["run_id"] == run_id for event in events)
    assert events[2]["payload"]["run_event_type"] == "reasoning"
    assert "reasoning_meta" not in str(events)


def test_websocket_excludes_streaming_delta_events(tmp_path: Path) -> None:
    adapter = StubAdapter(
        stream_deltas=[
            {"type": "reasoning_delta", "text": "Thinking"},
            {"type": "content_delta", "text": "Hello"},
        ]
    )
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, adapter)))

    with TestClient(app) as client:
        client.post(
            "/api/rpc",
            json={
                "method": "session.create",
                "params": {"agent_id": "coder", "session_id": "session-one"},
            },
        )
        with client.websocket_connect("/ws") as websocket:
            response = client.post(
                "/api/rpc",
                json={
                    "method": "chat.stream",
                    "params": {"agent_id": "coder", "session_id": "session-one", "content": "Hi"},
                },
            )
            run_id = response.json()["result"]["run_id"]
            events = [websocket.receive_json() for _ in range(5)]

    assert [event["type"] for event in events] == [
        "run_started",
        "run_output",
        "run_output",
        "run_output",
        "run_completed",
    ]
    assert [event["payload"]["run_event_type"] for event in events] == [
        "run_started",
        "user_message_persisted",
        "reasoning",
        "assistant_output",
        "run_completed",
    ]
    assert all(event["payload"]["run_id"] == run_id for event in events)
    assert "reasoning_delta" not in str(events)
    assert "assistant_output_delta" not in str(events)
    assert "tool_call_delta" not in str(events)


def test_websocket_output_mappings_exclude_streaming_delta_event_types() -> None:
    assert RUN_DELTA_EVENT_TYPES.isdisjoint(RUN_OUTPUT_EVENT_TYPES)
    assert RUN_DELTA_EVENT_TYPES.isdisjoint(SERVER_EVENT_TYPES)


def test_websocket_disconnect_removes_event_bus_subscriber(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws"):
            assert app.state.event_bus.subscriber_count == 1

        app.state.event_bus.publish("run_started", {"run_id": "run-one"})

    assert app.state.event_bus.subscriber_count == 0


def test_websocket_receives_agent_created_event_via_rpc(tmp_path: Path) -> None:
    """Agent CRUD events published by RPC delegates stream over WebSocket."""
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        response = client.post(
            "/api/rpc",
            json={"method": "agent.create", "params": {"id": "writer", "name": "Writer"}},
        )
        assert response.json()["ok"] is True
        event = websocket.receive_json()

    assert event["type"] == "agent.created"
    assert event["payload"]["id"] == "writer"


def test_server_event_contract_allows_app_error_events() -> None:
    bus = ServerEventBus()

    event = bus.publish(APP_ERROR_EVENT, {"message": "Background task failed"})

    assert APP_ERROR_EVENT in ALLOWED_SERVER_EVENT_TYPES
    assert event["type"] == APP_ERROR_EVENT
    assert event["payload"] == {"message": "Background task failed"}


def test_websocket_receives_app_error_events(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        app.state.event_bus.publish(APP_ERROR_EVENT, {"message": "Background task failed"})
        event = websocket.receive_json()

    assert event["type"] == APP_ERROR_EVENT
    assert event["payload"] == {"message": "Background task failed"}


def test_server_event_contract_rejects_unknown_events() -> None:
    bus = ServerEventBus()

    with pytest.raises(ValueError, match="unsupported server event type"):
        bus.publish("unknown.event", {"message": "No contract"})


def test_websocket_with_after_sequence_param_connects_successfully(tmp_path: Path) -> None:
    """WebSocket connects with after_sequence query param; subscriber is active."""
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws?after_sequence=3"):
            assert app.state.event_bus.subscriber_count == 1

        assert app.state.event_bus.subscriber_count == 0


def test_log_websocket_streams_append_events_for_selected_file(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    log_file.write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\n",
        encoding="utf-8",
    )
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/logs?file=2026-05-11") as websocket,
    ):
        assert app.state.event_bus.subscriber_count == 0
        assert app.state.log_viewer.watcher_count == 1
        assert app.state.log_viewer.subscriber_count("2026-05-11") == 1

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

    _close_log_viewer_now(app)

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

    _close_log_viewer_now(app)

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

    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/logs?file=2026-05-11") as websocket,
    ):
        log_file.write_text(
            "2026-05-11 09:00:02 [WARN] vbot.server.app - Reset\n",
            encoding="utf-8",
        )

        event = websocket.receive_json()
        websocket.close()

    _close_log_viewer_now(app)

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

    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/logs?file=2026-05-11") as websocket,
    ):
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

    _close_log_viewer_now(app)

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

    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/logs?file=2026-05-11") as websocket,
    ):
        log_file.write_text(
            "\n".join(
                [
                    "2026-05-11 09:00:02 [INFO] vbot.server.uvicorn - "
                    '127.0.0.1:60756 - "WebSocket /ws/logs?cursor=abc" [accepted]',
                    "2026-05-11 09:00:03 [INFO] vbot.server.uvicorn - connection closed",
                    "2026-05-11 09:00:04 [ERROR] vbot.server.uvicorn - opening handshake failed",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        event = websocket.receive_json()
        websocket.close()

    _close_log_viewer_now(app)

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
        with client.websocket_connect("/ws/logs?file=2026-05-11"):
            assert app.state.log_viewer.watcher_count == 1
            assert app.state.log_viewer.subscriber_count("2026-05-11") == 1

        deadline = time.time() + 2
        while time.time() < deadline and app.state.log_viewer.watcher_count != 0:
            time.sleep(0.05)

    _close_log_viewer_now(app)

    assert app.state.log_viewer.watcher_count == 0


# -- Unit tests for _parse_after_sequence --


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0),
        ("0", 0),
        ("5", 5),
        ("100", 100),
        ("-1", 0),
        ("-999", 0),
        ("abc", 0),
        ("3.14", 0),
        ("", 0),
    ],
)
def test_parse_after_sequence_valid_and_invalid_inputs(raw: str | None, expected: int) -> None:
    assert _parse_after_sequence(raw) == expected


# -- Unit tests for ServerEventBus.subscribe with after_sequence --


@pytest.mark.asyncio
async def test_event_bus_subscribe_replays_only_newer_events() -> None:
    bus = ServerEventBus()
    bus.publish("agent.created", {"id": "a"})  # sequence 1
    bus.publish("agent.updated", {"id": "a"})  # sequence 2
    bus.publish("agent.deleted", {"agent_id": "a"})  # sequence 3

    events: list[dict[str, Any]] = []
    async for event in bus.subscribe(after_sequence=1):
        events.append(event)
        if len(events) == 2:
            break

    assert events[0]["sequence"] == 2
    assert events[0]["type"] == "agent.updated"
    assert events[1]["sequence"] == 3
    assert events[1]["type"] == "agent.deleted"


@pytest.mark.asyncio
async def test_event_bus_subscribe_after_sequence_zero_receives_all_events() -> None:
    bus = ServerEventBus()
    bus.publish("agent.created", {"id": "a"})

    events: list[dict[str, Any]] = []
    async for event in bus.subscribe(after_sequence=0):
        events.append(event)
        break

    assert len(events) == 1
    assert events[0]["sequence"] == 1
    assert events[0]["type"] == "agent.created"


@pytest.mark.asyncio
async def test_event_bus_subscribe_after_sequence_higher_skips_all_replays() -> None:
    """When after_sequence exceeds all existing sequences, no events are replayed.
    The subscriber goes straight to the live subscription loop."""
    bus = ServerEventBus()
    bus.publish("agent.created", {"id": "a"})  # sequence 1

    async with aclosing(bus.subscribe(after_sequence=100)) as gen:
        # No replayed events — __anext__ enters the live queue wait immediately.
        # Use wait_for to confirm it does NOT return a replayed event quickly.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(gen.__anext__(), timeout=0.25)

    # Generator closed — subscriber removed
    assert bus.subscriber_count == 0
