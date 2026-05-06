"""Tests for WebSocket server event push."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from server.app import _parse_after_sequence, create_app
from server.events import ServerEventBus
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


def test_websocket_with_after_sequence_param_connects_successfully(tmp_path: Path) -> None:
    """WebSocket connects with after_sequence query param; subscriber is active."""
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        with client.websocket_connect("/ws?after_sequence=3"):
            assert app.state.event_bus.subscriber_count == 1

        assert app.state.event_bus.subscriber_count == 0


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
