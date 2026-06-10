"""Tests for WebSocket server event push."""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.runs import ChatRunManager, RunStatus
from core.subagents import SUBAGENT_SESSION_STARTED_EVENT
from server.app import _parse_after_sequence, create_app
from server.delegates import RUN_DELTA_EVENT_TYPES, RUN_OUTPUT_EVENT_TYPES, SERVER_EVENT_TYPES
from server.events import ALLOWED_SERVER_EVENT_TYPES, APP_ERROR_EVENT, ServerEventBus
from tests.server.test_rpc import StubAdapter, StubRuntime


def test_websocket_receives_run_lifecycle_events_without_provider_metadata(tmp_path: Path) -> None:
    adapter = StubAdapter(
        stream_deltas=[
            {"type": "reasoning_delta", "text": "Readable thinking"},
            {"type": "reasoning_meta", "reasoning_meta": {"secret": "opaque"}},
            {
                "type": "content_delta",
                "text": "Hello",
            },
            {"type": "finish", "reason": "stop"},
        ]
    )
    runtime = StubRuntime(tmp_path, adapter)
    runtime.agents.update("coder", model="openai/gpt-5.2::api-key")
    app = create_app(runtime=cast(Any, runtime))

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
            # First frame is the connection_ready hello; skip it.
            hello = websocket.receive_json()
            assert hello["type"] == "connection_ready"
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
            {"type": "finish", "reason": "stop"},
        ]
    )
    runtime = StubRuntime(tmp_path, adapter)
    runtime.agents.update("coder", model="openai/gpt-5.2::api-key")
    app = create_app(runtime=cast(Any, runtime))

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
            # First frame is the connection_ready hello; skip it.
            hello = websocket.receive_json()
            assert hello["type"] == "connection_ready"
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


def test_websocket_output_mappings_include_subagent_session_started() -> None:
    assert SUBAGENT_SESSION_STARTED_EVENT in RUN_OUTPUT_EVENT_TYPES


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
        # First frame is the connection_ready hello; skip it.
        hello = websocket.receive_json()
        assert hello["type"] == "connection_ready"

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
        # First frame is the connection_ready hello; skip it.
        hello = websocket.receive_json()
        assert hello["type"] == "connection_ready"

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
async def test_event_bus_replay_window_is_bounded_without_reusing_sequences() -> None:
    bus = ServerEventBus(event_retention_limit=2)
    bus.publish("agent.created", {"id": "a"})
    bus.publish("agent.updated", {"id": "a"})
    bus.publish("agent.deleted", {"agent_id": "a"})

    events: list[dict[str, Any]] = []
    async for event in bus.subscribe(after_sequence=0):
        events.append(event)
        if len(events) == 2:
            break

    assert [event["sequence"] for event in bus.events] == [2, 3]
    assert [event["sequence"] for event in events] == [2, 3]
    assert [event["type"] for event in events] == ["agent.updated", "agent.deleted"]


@pytest.mark.asyncio
async def test_event_bus_evicts_lagging_live_subscriber() -> None:
    bus = ServerEventBus(subscriber_queue_limit=2)

    async with aclosing(bus.subscribe()) as gen:
        first_event_task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0)

        bus.publish("agent.created", {"id": "a"})
        first_event = await first_event_task

        bus.publish("agent.updated", {"id": "a"})
        bus.publish("agent.updated", {"id": "a"})
        bus.publish("agent.deleted", {"agent_id": "a"})

        assert first_event["type"] == "agent.created"
        assert bus.subscriber_count == 0
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()


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


# -- Connection-ready handshake tests (Phase 1.1, Task 2) --


def _override_bus_epoch(bus: ServerEventBus, *, epoch: str | None = None) -> str:
    """Return the bus's epoch, optionally overriding it for the test.

    The bus's ``epoch`` is a read-only property backed by ``_epoch``. Tests
    that need a known epoch value mutate ``_epoch`` directly; tests that just
    want to learn the bus's current epoch leave it alone and read the
    property.
    """
    if epoch is not None:
        bus._epoch = epoch  # type: ignore[attr-defined]
    return bus.epoch


def _attach_chat_runs_active_runs(chat_runs: ChatRunManager) -> list[Any]:
    """Add a test-only ``active_runs()`` accessor to a ChatRunManager.

    Task 3 introduces the real method; this shim mirrors the same return shape
    so the handshake tests can run in isolation.
    """
    snapshot: list[Any] = []

    def active_runs() -> list[Any]:
        return list(snapshot)

    chat_runs.active_runs = active_runs  # type: ignore[method-assign]
    return snapshot


def test_websocket_handshake_sends_connection_ready_frame_with_no_pre_connect_replay(
    tmp_path: Path,
) -> None:
    """A fresh /ws connect receives a connection_ready hello first; pre-connect
    bus events are *not* re-delivered afterwards. Live events published after
    the connect still flow to the client."""
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        bus = app.state.event_bus
        bus_epoch = _override_bus_epoch(bus, epoch="epoch-abc")

        for index in range(3):
            bus.publish("agent.created", {"id": f"pre-{index}"})

        with client.websocket_connect("/ws") as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "connection_ready"
            assert hello["epoch"] == bus_epoch
            assert hello["last_sequence"] == 3
            assert hello["active_runs"] == []
            # Critical: no "sequence" field on the hello frame — it must not feed
            # the client's lastSequence bookkeeping.
            assert "sequence" not in hello

            bus.publish("agent.created", {"id": "post-0"})
            bus.publish("agent.updated", {"id": "post-0"})

            first_live = websocket.receive_json()
            second_live = websocket.receive_json()

    assert first_live["type"] == "agent.created"
    assert first_live["payload"] == {"id": "post-0"}
    assert first_live["sequence"] == 4
    assert second_live["type"] == "agent.updated"
    assert second_live["sequence"] == 5


def test_websocket_handshake_replays_when_epoch_and_after_sequence_match(
    tmp_path: Path,
) -> None:
    """Resume path: same-epoch + after_sequence>0 replays retained events newer
    than the client's marker, then continues with live events."""
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        bus = app.state.event_bus
        bus_epoch = _override_bus_epoch(bus, epoch="epoch-abc")

        for index in range(5):
            bus.publish("agent.created", {"id": f"event-{index + 1}"})

        with client.websocket_connect(f"/ws?after_sequence=3&epoch={bus_epoch}") as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "connection_ready"
            assert hello["epoch"] == bus_epoch
            assert hello["last_sequence"] == 5

            replayed = [websocket.receive_json() for _ in range(2)]
            assert [event["sequence"] for event in replayed] == [4, 5]
            assert [event["payload"]["id"] for event in replayed] == [
                "event-4",
                "event-5",
            ]


def test_websocket_handshake_live_only_for_stale_or_missing_epoch(
    tmp_path: Path,
) -> None:
    """B1 regression (server half): a stale or missing epoch must not strand
    the client. The hello frame is still sent, and only events published
    *after* the connect are delivered (no historical replay)."""
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        bus = app.state.event_bus
        bus_epoch = _override_bus_epoch(bus, epoch="epoch-abc")

        for index in range(5):
            bus.publish("agent.created", {"id": f"event-{index + 1}"})

        # Stale epoch path: client passes a different (older) epoch string.
        with client.websocket_connect("/ws?after_sequence=3000&epoch=stale-epoch") as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "connection_ready"
            assert hello["epoch"] == bus_epoch
            assert hello["last_sequence"] == 5

            bus.publish("agent.created", {"id": "live-1"})
            bus.publish("agent.updated", {"id": "live-2"})

            live_events = [websocket.receive_json() for _ in range(2)]
            assert [event["sequence"] for event in live_events] == [6, 7]
            assert [event["payload"]["id"] for event in live_events] == [
                "live-1",
                "live-2",
            ]

    # Build a fresh app so the bus is empty (no retained events to begin with).
    app2 = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app2) as client:
        bus2 = app2.state.event_bus
        bus2_epoch = _override_bus_epoch(bus2, epoch="epoch-abc")

        # Missing epoch path: client sends only after_sequence=3000, no epoch.
        with client.websocket_connect("/ws?after_sequence=3000") as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "connection_ready"
            assert hello["epoch"] == bus2_epoch
            assert hello["last_sequence"] == 0

            bus2.publish("agent.created", {"id": "live-1"})

            live_event = websocket.receive_json()
            assert live_event["sequence"] == 1
            assert live_event["payload"] == {"id": "live-1"}


def test_websocket_handshake_active_runs_lists_running_with_sse_url_and_omits_terminal(
    tmp_path: Path,
) -> None:
    """The connection_ready.active_runs snapshot lists only running runs and
    exposes the SSE endpoint URL for each."""
    runtime = StubRuntime(tmp_path, StubAdapter())
    app = create_app(runtime=cast(Any, runtime))

    running_run = cast(
        Any,
        type(
            "StubRun",
            (),
            {
                "id": "run-running",
                "agent_id": "coder",
                "session_id": "session-running",
                "status": RunStatus.RUNNING,
            },
        )(),
    )
    terminal_run = cast(
        Any,
        type(
            "StubRun",
            (),
            {
                "id": "run-terminal",
                "agent_id": "coder",
                "session_id": "session-terminal",
                "status": RunStatus.COMPLETED,
            },
        )(),
    )

    with TestClient(app) as client:
        _override_bus_epoch(app.state.event_bus, epoch="epoch-abc")

        chat_runs: ChatRunManager = app.state.chat_runs
        active_snapshot = _attach_chat_runs_active_runs(chat_runs)
        active_snapshot.extend([running_run, terminal_run])

        with client.websocket_connect("/ws") as websocket:
            hello = websocket.receive_json()

    assert hello["type"] == "connection_ready"
    assert hello["active_runs"] == [
        {
            "run_id": "run-running",
            "agent_id": "coder",
            "session_id": "session-running",
            "status": "running",
            "sse_url": "/api/runs/run-running/events",
        }
    ]
