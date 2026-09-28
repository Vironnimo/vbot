"""Tests for the /ws connection_ready handshake: replay cursor and reconnect snapshot."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]
from starlette.websockets import WebSocketDisconnect  # type: ignore[import-not-found]

from core.runs import RunKind, RunStatus
from core.sessions import SessionAddress
from server.app import create_app
from server.events import APP_ERROR_EVENT, ServerEventBus
from tests.server.rpc_test_support import StubAdapter, StubRuntime


def _set_bus_epoch(bus: ServerEventBus, epoch: str) -> None:
    """Give the bus a known epoch; the public ``epoch`` property is read-only."""
    bus._epoch = epoch  # type: ignore[attr-defined]


def _stub_app(tmp_path: Path) -> Any:
    return create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))


def test_websocket_handshake_sends_connection_ready_frame_with_no_pre_connect_replay(
    tmp_path: Path,
) -> None:
    """A fresh /ws connect receives a connection_ready hello first; pre-connect
    bus events are *not* re-delivered afterwards. Live events published after
    the connect still flow to the client."""
    app = _stub_app(tmp_path)

    with TestClient(app) as client:
        bus = app.state.event_bus
        _set_bus_epoch(bus, "epoch-abc")

        for index in range(3):
            bus.publish("run_started", {"id": f"pre-{index}"})

        with client.websocket_connect("/ws") as websocket:
            hello = websocket.receive_json()
            bus.publish("run_started", {"id": "post-0"})
            bus.publish("run_output", {"id": "post-0"})
            first_live = websocket.receive_json()
            second_live = websocket.receive_json()

    # Sequence 4 is this window's own presence connect signal, published on
    # register before the hello read; the 3 pre-connect run events are not replayed.
    assert hello == {
        "type": "connection_ready",
        "epoch": "epoch-abc",
        "last_sequence": 4,
        "replay_status": "fresh",
        "active_runs": [],
        "queues": [],
    }
    # No "sequence" field on the hello: it must not feed the client's
    # lastSequence bookkeeping.
    assert "sequence" not in hello
    assert (first_live["type"], first_live["payload"], first_live["sequence"]) == (
        "run_started",
        {"id": "post-0"},
        5,
    )
    assert (second_live["type"], second_live["sequence"]) == ("run_output", 6)


def test_websocket_handshake_replays_when_epoch_and_after_sequence_match(
    tmp_path: Path,
) -> None:
    """Resume path: same-epoch + after_sequence>0 replays retained events newer
    than the client's marker, then continues with live events."""
    app = _stub_app(tmp_path)

    with TestClient(app) as client:
        bus = app.state.event_bus
        _set_bus_epoch(bus, "epoch-abc")

        for index in range(5):
            bus.publish("run_started", {"id": f"event-{index + 1}"})

        # Surrounding whitespace around the query values is ignored.
        with client.websocket_connect("/ws?after_sequence=3&epoch=%20epoch-abc%20") as websocket:
            hello = websocket.receive_json()
            replayed = [websocket.receive_json() for _ in range(2)]

    assert hello["epoch"] == "epoch-abc"
    # 6 = 5 retained run events + this window's own presence connect signal.
    assert hello["last_sequence"] == 6
    assert hello["replay_status"] == "resumed"
    assert [(event["sequence"], event["payload"]["id"]) for event in replayed] == [
        (4, "event-4"),
        (5, "event-5"),
    ]


def test_websocket_handshake_live_only_for_stale_or_missing_epoch(
    tmp_path: Path,
) -> None:
    """B1 regression (server half): a stale or missing epoch must not strand
    the client. The hello frame is still sent, and only events published
    *after* the connect are delivered (no historical replay)."""
    app = _stub_app(tmp_path)
    connections: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    with TestClient(app) as client:
        bus = app.state.event_bus
        _set_bus_epoch(bus, "epoch-abc")
        for index in range(5):
            bus.publish("run_started", {"id": f"event-{index + 1}"})

        for name, query in (
            ("stale", "after_sequence=3000&epoch=stale-epoch"),
            ("missing", "after_sequence=3000"),
        ):
            with client.websocket_connect(f"/ws?{query}") as websocket:
                hello = websocket.receive_json()
                bus.publish("run_started", {"id": f"live-{name}"})
                connections[name] = (hello, websocket.receive_json())

    stale_hello, stale_live = connections["stale"]
    missing_hello, missing_live = connections["missing"]
    assert (stale_hello["epoch"], stale_hello["replay_status"]) == ("epoch-abc", "epoch_changed")
    assert (missing_hello["epoch"], missing_hello["replay_status"]) == ("epoch-abc", "fresh")
    for hello, live, name in (
        (stale_hello, stale_live, "stale"),
        (missing_hello, missing_live, "missing"),
    ):
        assert live["sequence"] == hello["last_sequence"] + 1
        assert live["payload"] == {"id": f"live-{name}"}


def test_websocket_handshake_reports_replay_gap_without_partial_replay(tmp_path: Path) -> None:
    """A cursor that fell out of retention, or that is ahead of the bus, is a
    gap: no partial replay, live events only. A cursor right before the oldest
    retained event still resumes."""
    app = _stub_app(tmp_path)
    connections: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}

    with TestClient(app) as client:
        bus = ServerEventBus(event_retention_limit=2)
        _set_bus_epoch(bus, "epoch-abc")
        app.state.event_bus = bus
        for index in range(4):
            bus.publish("run_started", {"id": f"event-{index + 1}"})

        # Each connect first publishes its own presence signal, so the bus
        # retains [last_sequence - 1, last_sequence] at the hello.
        for cursor in (3, 2, 99):
            with client.websocket_connect(f"/ws?after_sequence={cursor}&epoch=epoch-abc") as ws:
                hello = ws.receive_json()
                bus.publish("run_started", {"id": f"live-{cursor}"})
                connections[cursor] = (hello, ws.receive_json())
            if cursor == 3:
                assert hello["last_sequence"] == 5

    resumed_hello, first_resumed = connections[3]
    assert resumed_hello["replay_status"] == "resumed"
    assert first_resumed["sequence"] == 4
    for cursor in (2, 99):
        hello, first_event = connections[cursor]
        assert hello["replay_status"] == "gap"
        assert first_event["sequence"] == hello["last_sequence"] + 1
        assert first_event["payload"] == {"id": f"live-{cursor}"}


def _stub_run(run_id: str, **fields: Any) -> Any:
    values: dict[str, Any] = {
        "id": run_id,
        "agent_id": "coder",
        "project_id": None,
        "session_id": f"session-{run_id}",
        "run_kind": RunKind.USER,
        "status": RunStatus.RUNNING,
        "created_at": "2026-08-05T18:00:00+00:00",
        "iteration_count": 0,
        "last_sequence": 0,
        "controls": lambda: {"compaction": "unavailable", "background_tool_call_ids": []},
    }
    values.update(fields)
    return SimpleNamespace(**values)


class _QueuedItem:
    def __init__(self, item_id: str, *, internal: bool = False) -> None:
        self.item_id = item_id
        self.internal = internal

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.item_id, "internal": self.internal}


def test_websocket_hello_snapshots_running_runs_and_public_queues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hello lets a reconnecting window re-attach running Runs over SSE and
    rebuild its Queues without extra reads."""
    runs = [
        _stub_run(
            "run-running",
            project_id="acme",
            iteration_count=4,
            last_sequence=7,
        ),
        _stub_run("run-terminal", status=RunStatus.COMPLETED),
        # A review fork carries its source Session from the Run itself, so the
        # WebUI projects the review without a Session read.
        _stub_run(
            "run-refl",
            run_kind=RunKind.MEMORY_REFLECTION,
            source_session_id="session-source",
        ),
        _stub_run("run-system", run_kind=RunKind.SYSTEM, contributes_to_agent_activity=False),
    ]
    writer_session = SessionAddress(
        project_id="project-b", agent_id="writer", session_id="session-b"
    )
    coder_session = SessionAddress(project_id=None, agent_id="coder", session_id="session-a")
    queued = [
        (writer_session, _QueuedItem("second")),
        (coder_session, _QueuedItem("first")),
        (coder_session, _QueuedItem("hidden", internal=True)),
        (writer_session, _QueuedItem("third")),
    ]
    app = _stub_app(tmp_path)

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.chat_runs, "active_runs", lambda: list(runs))
        monkeypatch.setattr(app.state.chat_runs, "all_queued", lambda: list(queued))
        with client.websocket_connect("/ws") as websocket:
            hello = websocket.receive_json()

    def snapshot(run_id: str, **fields: Any) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "agent_id": "coder",
            "project_id": None,
            "session_id": f"session-{run_id}",
            "run_kind": "user",
            "status": "running",
            "started_at": "2026-08-05T18:00:00+00:00",
            "iteration_count": 0,
            "controls": {"compaction": "unavailable", "background_tool_call_ids": []},
            "controls_sequence": 0,
            "sse_url": f"/api/runs/{run_id}/events",
            **fields,
        }

    assert hello["active_runs"] == [
        snapshot("run-running", project_id="acme", iteration_count=4, controls_sequence=7),
        snapshot("run-refl", run_kind="memory_reflection", source_session_id="session-source"),
        snapshot("run-system", run_kind="system", contributes_to_agent_activity=False),
    ]
    assert hello["queues"] == [
        {
            "project_id": None,
            "agent_id": "coder",
            "session_id": "session-a",
            "items": [{"id": "first", "internal": False}],
        },
        {
            "project_id": "project-b",
            "agent_id": "writer",
            "session_id": "session-b",
            "items": [
                {"id": "second", "internal": False},
                {"id": "third", "internal": False},
            ],
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_mode", ["disconnect", "send_error", "send_cancel"])
async def test_shared_socket_closes_subscription_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_mode: str
) -> None:
    import server.app as server_app

    bus = ServerEventBus()
    app = _stub_app(tmp_path)
    app.state.event_bus = bus
    monkeypatch.setattr(server_app, "_register_ws_client", lambda _socket: None)
    monkeypatch.setattr(server_app, "_active_runs_snapshot", lambda _state: [])
    monkeypatch.setattr(server_app, "_queues_snapshot", lambda _state: [])
    held_streams: list[Any] = []
    original_subscribe = bus.subscribe

    def subscribe(**kwargs: Any) -> Any:
        stream = original_subscribe(**kwargs)
        held_streams.append(stream)  # Prevent GC from concealing a missing aclose.
        return stream

    monkeypatch.setattr(bus, "subscribe", subscribe)

    async def accept() -> None:
        pass

    async def receive() -> dict[str, Any]:
        while bus.subscriber_count == 0:
            await asyncio.sleep(0)
        if exit_mode == "disconnect":
            return {"type": "websocket.disconnect"}
        bus.publish(APP_ERROR_EVENT, {"message": "test-owned event"})
        await asyncio.Event().wait()
        return {}

    async def send_json(event: dict[str, Any]) -> None:
        if event["type"] == "connection_ready":
            return
        assert bus.subscriber_count == 1
        if exit_mode == "send_cancel":
            raise asyncio.CancelledError
        raise WebSocketDisconnect()

    socket = SimpleNamespace(
        app=app, query_params={}, accept=accept, receive=receive, send_json=send_json
    )
    endpoint = next(
        cast(Any, route).endpoint for route in app.routes if getattr(route, "path", None) == "/ws"
    )
    try:
        async with asyncio.timeout(2):
            if exit_mode == "send_cancel":
                with pytest.raises(asyncio.CancelledError):
                    await endpoint(socket)
            else:
                await endpoint(socket)
        assert held_streams
        assert bus.subscriber_count == 0
    finally:
        for stream in held_streams:
            await stream.aclose()
