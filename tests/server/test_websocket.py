"""Tests for websocket."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]
from starlette.websockets import WebSocketDisconnect  # type: ignore[import-not-found]

from core.runs import STREAM_ATTEMPT_RESTARTED_EVENT
from core.subagents import (
    SUBAGENT_SESSION_STARTED_EVENT,
    SUBAGENT_STATUS_CHANGED_EVENT,
)
from core.tools.terminal_manager import TerminalNotFoundError
from server.app import create_app
from server.events import ALLOWED_SERVER_EVENT_TYPES, APP_ERROR_EVENT, ServerEventBus
from server.rpc.event_bridge import (
    RUN_DELTA_EVENT_TYPES,
    RUN_OUTPUT_EVENT_TYPES,
    SERVER_EVENT_TYPES,
)
from tests.server.test_rpc import StubAdapter, StubRuntime


@pytest.mark.parametrize(
    "path",
    [
        "/ws",
        "/ws/logs?file=2026-08-10",
        "/ws/terminals/term-1",
    ],
)
def test_websocket_transport_rejects_cross_origin_browser_connections(
    tmp_path: Path,
    path: str,
) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(
            path,
            headers={"origin": "https://attacker.example"},
        ),
    ):
        pass

    assert exc_info.value.code == 1008


def test_websocket_transport_allows_same_origin_browser_connection(tmp_path: Path) -> None:
    app = create_app(
        runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())),
        server_bind={
            "listen_host": "0.0.0.0",
            "listen_port": 8420,
            "port_source": "cli",
        },
    )

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/ws",
            headers={
                "host": "192.168.10.25:8420",
                "origin": "http://192.168.10.25:8420",
            },
        ) as websocket,
    ):
        hello = websocket.receive_json()

    assert hello["type"] == "connection_ready"


def test_terminal_websocket_sends_snapshot_then_live_output_and_terminal_state(
    tmp_path: Path,
) -> None:
    runtime = StubRuntime(tmp_path, StubAdapter())
    terminal_manager = StubTerminalWebsocketManager()
    runtime.terminal_manager = cast(Any, terminal_manager)
    app = create_app(runtime=cast(Any, runtime))

    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/terminals/term-1") as websocket,
    ):
        events = [websocket.receive_json() for _ in range(3)]

    assert events == [
        {
            "type": "terminal_ready",
            "sequence": 2,
            "terminal": {"terminal_id": "term-1", "state": "working"},
            "ansi": "\x1b[2Jready",
        },
        {"type": "terminal_output", "sequence": 3, "data": "next"},
        {
            "type": "terminal_state",
            "sequence": 4,
            "terminal": {"terminal_id": "term-1", "state": "exited"},
        },
    ]
    assert terminal_manager.unsubscribed


class StubTerminalWebsocketManager:
    def __init__(self) -> None:
        self.unsubscribed = False

    def add_changed_callback(self, _callback: Any) -> Any:
        def unsubscribe() -> None:
            self.unsubscribed = True

        return unsubscribe

    async def watch_for_operator(self, terminal_id: str) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "terminal_ready",
            "sequence": 2,
            "terminal": {"terminal_id": terminal_id, "state": "working"},
            "ansi": "\x1b[2Jready",
        }
        yield {"type": "terminal_output", "sequence": 3, "data": "next"}
        yield {
            "type": "terminal_state",
            "sequence": 4,
            "terminal": {"terminal_id": terminal_id, "state": "exited"},
        }


class MissingTerminalWebsocketManager(StubTerminalWebsocketManager):
    def watch_for_operator(self, terminal_id: str) -> AsyncIterator[dict[str, Any]]:
        raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")


def test_terminal_websocket_closes_cleanly_when_subscription_setup_fails(
    tmp_path: Path,
) -> None:
    runtime = StubRuntime(tmp_path, StubAdapter())
    runtime.terminal_manager = cast(Any, MissingTerminalWebsocketManager())
    app = create_app(runtime=cast(Any, runtime))

    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/terminals/missing") as websocket,
        pytest.raises(WebSocketDisconnect) as exc_info,
    ):
        websocket.receive_json()

    assert exc_info.value.code == 1008


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
            events = [websocket.receive_json() for _ in range(7)]

    assert [event["type"] for event in events] == [
        "resource_changed",
        "run_started",
        "run_output",
        "run_output",
        "run_output",
        "run_output",
        "run_completed",
    ]
    assert events[0]["payload"]["kind"] == "agents"
    assert all(event["payload"]["run_id"] == run_id for event in events[1:])
    assert events[3]["payload"]["run_event_type"] == "reasoning"
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
            events = [websocket.receive_json() for _ in range(7)]

    assert [event["type"] for event in events] == [
        "resource_changed",
        "run_started",
        "run_output",
        "run_output",
        "run_output",
        "run_output",
        "run_completed",
    ]
    assert events[0]["payload"]["kind"] == "agents"
    assert [event["payload"]["run_event_type"] for event in events[1:]] == [
        "run_started",
        "user_message_persisted",
        "reasoning",
        "assistant_output",
        "model_step_usage",
        "run_completed",
    ]
    assert all(event["payload"]["run_id"] == run_id for event in events[1:])
    assert "reasoning_delta" not in str(events)
    assert "assistant_output_delta" not in str(events)
    assert "tool_call_delta" not in str(events)


def test_websocket_output_mappings_exclude_streaming_delta_event_types() -> None:
    assert RUN_DELTA_EVENT_TYPES.isdisjoint(RUN_OUTPUT_EVENT_TYPES)
    assert STREAM_ATTEMPT_RESTARTED_EVENT in RUN_DELTA_EVENT_TYPES
    assert RUN_DELTA_EVENT_TYPES.isdisjoint(SERVER_EVENT_TYPES)


def test_websocket_output_mappings_include_subagent_lifecycle_events() -> None:
    assert SUBAGENT_SESSION_STARTED_EVENT in RUN_OUTPUT_EVENT_TYPES
    assert SUBAGENT_STATUS_CHANGED_EVENT in RUN_OUTPUT_EVENT_TYPES


def test_websocket_disconnect_removes_event_bus_subscriber(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws"):
            wait_for_event_bus_subscribers(app, expected_count=1)

        app.state.event_bus.publish("run_started", {"run_id": "run-one"})
        wait_for_event_bus_subscribers(app, expected_count=0)

    assert app.state.event_bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_websocket_disconnect_during_hello_unregisters_client(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))
    endpoint = next(
        cast(Any, route).endpoint for route in app.routes if getattr(route, "path", None) == "/ws"
    )

    class DisconnectingWebSocket:
        def __init__(self) -> None:
            self.app = app
            self.query_params: dict[str, str] = {
                "connection_id": "disconnecting-tab",
                "accessor": "browser",
            }
            self.headers: dict[str, str] = {}

        async def accept(self) -> None:
            return None

        async def send_json(self, _payload: Any) -> None:
            raise WebSocketDisconnect(code=1001)

    with TestClient(app):
        await endpoint(DisconnectingWebSocket())
        assert app.state.client_registry.list() == []


@pytest.mark.asyncio
async def test_idle_websocket_disconnect_unregisters_client(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))
    endpoint = next(
        cast(Any, route).endpoint for route in app.routes if getattr(route, "path", None) == "/ws"
    )

    class IdleDisconnectingWebSocket:
        def __init__(self) -> None:
            self.app = app
            self.query_params: dict[str, str] = {
                "connection_id": "idle-tab",
                "accessor": "browser",
            }
            self.headers: dict[str, str] = {}
            self.sent: list[dict[str, Any]] = []

        async def accept(self) -> None:
            return None

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def receive(self) -> dict[str, Any]:
            return {"type": "websocket.disconnect", "code": 1000}

    websocket = IdleDisconnectingWebSocket()
    with TestClient(app):
        await endpoint(websocket)

    assert websocket.sent[0]["type"] == "connection_ready"
    assert app.state.client_registry.list() == []
    assert [
        event["payload"]
        for event in app.state.event_bus.events
        if event["type"] == "resource_changed"
    ] == [{"kind": "clients"}, {"kind": "clients"}]


def test_websocket_receives_resource_changed_on_agent_create_via_rpc(tmp_path: Path) -> None:
    """Agent CRUD rides the generic reload-on-change channel: agent.create
    publishes resource_changed(kind="agents") over /ws, carrying no agent data
    (open windows re-fetch agent.list), not a dedicated agent.created event."""
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

    assert event["type"] == "resource_changed"
    assert event["payload"] == {"kind": "agents"}


def test_websocket_connect_publishes_clients_and_client_list_reflects_roster(
    tmp_path: Path,
) -> None:
    """A connecting app window registers in the presence roster, publishes a
    clients resource_changed signal, and shows up in client.list with the
    derived browser/OS and its client-minted connection id."""
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/ws?connection_id=tab-a&accessor=browser",
            headers={"user-agent": user_agent},
        ) as websocket,
    ):
        # Receiving the hello proves register() ran (it precedes the hello
        # send), so client.list is guaranteed to see the entry. The window does
        # NOT replay its own connect signal, so we must not wait for one here.
        assert websocket.receive_json()["type"] == "connection_ready"

        response = client.post("/api/rpc", json={"method": "client.list"})

    result = response.json()["result"]
    assert len(result["clients"]) == 1
    entry = result["clients"][0]
    assert entry["connection_id"] == "tab-a"
    assert entry["accessor"] == "browser"
    assert entry["browser"] == "Chrome"
    assert entry["os"] == "Windows"
    assert entry["status"] == "connected"


def test_websocket_disconnect_publishes_clients_to_other_windows(tmp_path: Path) -> None:
    """Another window connecting and disconnecting each push a clients
    resource_changed signal to an already-open window, and the disconnect drops
    the entry from client.list."""
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with (
        TestClient(app) as client,
        client.websocket_connect("/ws?connection_id=tab-a&accessor=browser") as window_a,
    ):
        # window_a does NOT receive its own connect signal (no self-replay), so
        # after the hello it only hears about *other* windows.
        assert window_a.receive_json()["type"] == "connection_ready"

        with client.websocket_connect("/ws?connection_id=tab-b&accessor=desktop") as window_b:
            assert window_b.receive_json()["type"] == "connection_ready"
            # window_a observes window_b joining.
            connect_signal = window_a.receive_json()
            assert connect_signal["type"] == "resource_changed"
            assert connect_signal["payload"] == {"kind": "clients"}

            both = client.post("/api/rpc", json={"method": "client.list"})
            assert {entry["connection_id"] for entry in both.json()["result"]["clients"]} == {
                "tab-a",
                "tab-b",
            }

        # window_b disconnected: window_a observes it leaving and the roster
        # drops back to just window_a.
        disconnect_signal = window_a.receive_json()
        assert disconnect_signal["type"] == "resource_changed"
        assert disconnect_signal["payload"] == {"kind": "clients"}

        remaining = client.post("/api/rpc", json={"method": "client.list"})

    assert [entry["connection_id"] for entry in remaining.json()["result"]["clients"]] == ["tab-a"]


def test_client_list_empty_before_any_window_connects(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        response = client.post("/api/rpc", json={"method": "client.list"})

    assert response.json()["result"] == {"clients": []}


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

    with pytest.raises(ValueError):
        bus.publish("unknown.event", {"message": "No contract"})


def test_websocket_with_after_sequence_param_connects_successfully(tmp_path: Path) -> None:
    """WebSocket connects with after_sequence query param; subscriber is active."""
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws?after_sequence=3") as websocket:
            hello = websocket.receive_json()
            app.state.event_bus.publish(APP_ERROR_EVENT, {"message": "Connected"})
            event = websocket.receive_json()

            assert hello["type"] == "connection_ready"
            assert event["type"] == APP_ERROR_EVENT
            assert event["payload"] == {"message": "Connected"}

        assert app.state.event_bus.subscriber_count == 0


def wait_for_event_bus_subscribers(
    app: Any, *, expected_count: int, timeout_seconds: float = 2.0
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if app.state.event_bus.subscriber_count == expected_count:
            return
        time.sleep(0.01)

    raise AssertionError(
        "timed out waiting for event-bus subscriber count "
        f"{expected_count}; got {app.state.event_bus.subscriber_count}"
    )
