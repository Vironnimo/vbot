"""Tests for the /ws event socket, window presence and the terminal socket."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]
from starlette.websockets import WebSocketDisconnect  # type: ignore[import-not-found]

from core.tools.terminal_manager import TerminalNotFoundError
from server.app import create_app
from server.events import APP_ERROR_EVENT, RUN_STARTED_SERVER_EVENT
from tests.server.rpc_test_support import StubAdapter, StubRuntime


def test_websocket_transports_reject_cross_origin_browsers_and_allow_the_server_origin(
    tmp_path: Path,
) -> None:
    app = create_app(
        runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())),
        server_bind={"listen_host": "0.0.0.0", "listen_port": 8420, "port_source": "cli"},
    )
    rejected_codes: dict[str, int] = {}

    with TestClient(app) as client:
        for path in ("/ws", "/ws/logs?file=2026-08-10", "/ws/terminals/term-1"):
            with (
                pytest.raises(WebSocketDisconnect) as exc_info,
                client.websocket_connect(path, headers={"origin": "https://attacker.example"}),
            ):
                pass
            rejected_codes[path] = exc_info.value.code
        with client.websocket_connect(
            "/ws",
            headers={"host": "192.168.10.25:8420", "origin": "http://192.168.10.25:8420"},
        ) as websocket:
            hello = websocket.receive_json()

    assert set(rejected_codes.values()) == {1008}
    assert len(rejected_codes) == 3
    assert hello["type"] == "connection_ready"


def test_websocket_forwards_run_lifecycle_without_deltas_or_provider_metadata(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter(
        stream_deltas=[
            {"type": "reasoning_delta", "text": "Readable thinking"},
            {"type": "reasoning_meta", "reasoning_meta": {"secret": "opaque"}},
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
            assert websocket.receive_json()["type"] == "connection_ready"
            events = [websocket.receive_json() for _ in range(9)]

    status_events = [
        event
        for event in events
        if event["payload"].get("run_event_type") == "provider_request_status"
    ]
    assert [event["payload"]["output"]["state"] for event in status_events] == [
        "waiting",
        "finished",
    ]
    events = [event for event in events if event not in status_events]
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
    # Streaming deltas stay on the Run's SSE stream; opaque provider metadata never leaves.
    hidden = ("reasoning_delta", "assistant_output_delta", "tool_call_delta", "reasoning_meta")
    assert [fragment for fragment in hidden if fragment in str(events)] == []


def test_websocket_forwards_bus_events_and_unsubscribes_on_disconnect(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            assert websocket.receive_json()["type"] == "connection_ready"
            app.state.event_bus.publish(APP_ERROR_EVENT, {"message": "Background task failed"})
            event = websocket.receive_json()

        app.state.event_bus.publish(RUN_STARTED_SERVER_EVENT, {"run_id": "run-one"})
        wait_for_event_bus_subscribers(app, expected_count=0)

    assert event["type"] == APP_ERROR_EVENT
    assert event["payload"] == {"message": "Background task failed"}


def test_window_presence_roster_follows_connects_and_disconnects(tmp_path: Path) -> None:
    """Each window registers in the presence roster with its client-minted
    connection id and derived browser/OS; joining and leaving push a clients
    resource_changed signal to the other open windows, never to the window
    itself."""
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def roster(client: TestClient) -> list[dict[str, Any]]:
        response = client.post("/api/rpc", json={"method": "client.list"})
        return cast(list[dict[str, Any]], response.json()["result"]["clients"])

    with TestClient(app) as client:
        before_any_window = roster(client)
        with client.websocket_connect(
            "/ws?connection_id=tab-a&accessor=browser", headers={"user-agent": user_agent}
        ) as window_a:
            # The hello follows registration, so the roster already lists the window.
            assert window_a.receive_json()["type"] == "connection_ready"
            [tab_a] = roster(client)

            with client.websocket_connect("/ws?connection_id=tab-b&accessor=desktop") as window_b:
                assert window_b.receive_json()["type"] == "connection_ready"
                join_signal = window_a.receive_json()
                both = {entry["connection_id"] for entry in roster(client)}

            leave_signal = window_a.receive_json()
            remaining = [entry["connection_id"] for entry in roster(client)]

    assert before_any_window == []
    expected_tab_a = {
        "connection_id": "tab-a",
        "accessor": "browser",
        "browser": "Chrome",
        "os": "Windows",
        "status": "connected",
    }
    assert {key: tab_a[key] for key in expected_tab_a} == expected_tab_a
    assert both == {"tab-a", "tab-b"}
    for signal in (join_signal, leave_signal):
        assert (signal["type"], signal["payload"]) == ("resource_changed", {"kind": "clients"})
    assert remaining == ["tab-a"]


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


class StubTerminalWebsocketManager:
    def __init__(self) -> None:
        self.unsubscribed = False

    def add_changed_callback(self, _callback: Any) -> Any:
        def unsubscribe() -> None:
            self.unsubscribed = True

        return unsubscribe

    def watch_for_operator(self, terminal_id: str) -> AsyncIterator[dict[str, Any]]:
        if terminal_id == "missing":
            raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")
        return self._frames(terminal_id)

    async def _frames(self, terminal_id: str) -> AsyncIterator[dict[str, Any]]:
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


def test_terminal_websocket_closes_cleanly_when_subscription_setup_fails(
    tmp_path: Path,
) -> None:
    runtime = StubRuntime(tmp_path, StubAdapter())
    runtime.terminal_manager = cast(Any, StubTerminalWebsocketManager())
    app = create_app(runtime=cast(Any, runtime))

    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/terminals/missing") as websocket,
        pytest.raises(WebSocketDisconnect) as exc_info,
    ):
        websocket.receive_json()

    assert exc_info.value.code == 1008


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
