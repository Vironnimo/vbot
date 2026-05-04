"""Tests for WebSocket server event push."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from server.app import create_app
from tests.server.test_rpc import StubAdapter, StubRuntime


def test_websocket_receives_run_lifecycle_events_without_provider_metadata(tmp_path: Path) -> None:
    adapter = StubAdapter(
        [
            {
                "content": "Hello",
                "reasoning": "Readable thinking",
                "reasoning_meta": {"secret": "opaque"},
                "tool_calls": None,
            }
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


def test_websocket_disconnect_removes_event_bus_subscriber(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws"):
            assert app.state.event_bus.subscriber_count == 1

        app.state.event_bus.publish("run_started", {"run_id": "run-one"})

    assert app.state.event_bus.subscriber_count == 0
