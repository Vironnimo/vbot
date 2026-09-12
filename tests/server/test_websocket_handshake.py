"""Tests for websocket handshake."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]
from starlette.websockets import WebSocketDisconnect  # type: ignore[import-not-found]

from core.runs import ChatRunManager, RunKind, RunStatus
from core.sessions import SessionAddress
from server.app import create_app
from server.events import APP_ERROR_EVENT, ServerEventBus
from tests.server.test_rpc import StubAdapter, StubRuntime


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
            bus.publish("run_started", {"id": f"pre-{index}"})

        with client.websocket_connect("/ws") as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "connection_ready"
            assert hello["epoch"] == bus_epoch
            # Sequence 4 is this window's own presence connect signal, published
            # on register before the hello read; the 3 pre-connect run events
            # (1..3) are still not replayed below.
            assert hello["last_sequence"] == 4
            assert hello["replay_status"] == "fresh"
            assert hello["active_runs"] == []
            assert hello["queues"] == []
            # Critical: no "sequence" field on the hello frame — it must not feed
            # the client's lastSequence bookkeeping.
            assert "sequence" not in hello

            bus.publish("run_started", {"id": "post-0"})
            bus.publish("run_output", {"id": "post-0"})

            first_live = websocket.receive_json()
            second_live = websocket.receive_json()

    assert first_live["type"] == "run_started"
    assert first_live["payload"] == {"id": "post-0"}
    assert first_live["sequence"] == 5
    assert second_live["type"] == "run_output"
    assert second_live["sequence"] == 6


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
            bus.publish("run_started", {"id": f"event-{index + 1}"})

        with client.websocket_connect(f"/ws?after_sequence=3&epoch={bus_epoch}") as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "connection_ready"
            assert hello["epoch"] == bus_epoch
            # 6 = 5 retained run events + this window's own presence connect
            # signal (published on register before the hello read).
            assert hello["last_sequence"] == 6
            assert hello["replay_status"] == "resumed"

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
            bus.publish("run_started", {"id": f"event-{index + 1}"})

        # Stale epoch path: client passes a different (older) epoch string.
        with client.websocket_connect("/ws?after_sequence=3000&epoch=stale-epoch") as websocket:
            hello = websocket.receive_json()
            assert hello["type"] == "connection_ready"
            assert hello["epoch"] == bus_epoch
            # 6 = 5 retained run events + this window's own presence connect
            # signal (published on register before the hello read).
            assert hello["last_sequence"] == 6
            assert hello["replay_status"] == "epoch_changed"

            bus.publish("run_started", {"id": "live-1"})
            bus.publish("run_output", {"id": "live-2"})

            live_events = [websocket.receive_json() for _ in range(2)]
            assert [event["sequence"] for event in live_events] == [7, 8]
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
            # 1 = this window's own presence connect signal (the only event so
            # far); the live-only floor sits above it so it is not replayed.
            assert hello["last_sequence"] == 1
            assert hello["replay_status"] == "fresh"

            bus2.publish("run_started", {"id": "live-1"})

            live_event = websocket.receive_json()
            assert live_event["sequence"] == 2
            assert live_event["payload"] == {"id": "live-1"}


def test_websocket_handshake_reports_replay_gap_without_partial_replay(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        bus = ServerEventBus(event_retention_limit=2)
        _override_bus_epoch(bus, epoch="epoch-abc")
        app.state.event_bus = bus
        for index in range(4):
            bus.publish("run_started", {"id": f"event-{index + 1}"})

        with client.websocket_connect("/ws?after_sequence=1&epoch=epoch-abc") as websocket:
            hello = websocket.receive_json()
            assert hello["replay_status"] == "gap"
            assert hello["last_sequence"] == 5

            bus.publish("run_started", {"id": "live"})
            first_event = websocket.receive_json()

    assert first_event["sequence"] == 6
    assert first_event["payload"] == {"id": "live"}


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
                "project_id": None,
                "session_id": "session-running",
                "run_kind": RunKind.USER,
                "status": RunStatus.RUNNING,
                "created_at": "2026-05-03T14:30:01+00:00",
                "iteration_count": 3,
                "events": [],
                "controls": lambda self: {
                    "compaction": "unavailable",
                    "background_tool_call_ids": [],
                },
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
            "project_id": None,
            "session_id": "session-running",
            "run_kind": "user",
            "status": "running",
            "started_at": "2026-05-03T14:30:01+00:00",
            "iteration_count": 3,
            "controls": {"compaction": "unavailable", "background_tool_call_ids": []},
            "controls_sequence": 0,
            "sse_url": "/api/runs/run-running/events",
        }
    ]


def test_websocket_handshake_reflection_run_carries_source_session(
    tmp_path: Path,
) -> None:
    """A running reflection Run's snapshot entry resolves the reviewed source
    Session from the fork's provenance sidecar so the WebUI can project the
    review onto its originating Session."""
    runtime = StubRuntime(tmp_path, StubAdapter())
    app = create_app(runtime=cast(Any, runtime))

    sessions = runtime.chat_sessions
    source = sessions.create("coder", session_id="session-source")
    fork = sessions.create("coder", session_id="session-fork")
    sessions.set_metadata(
        SessionAddress(project_id=None, agent_id="coder", session_id=fork.id),
        {"fork_source": {"agent_id": "coder", "session_id": source.id}},
    )

    reflection_run = cast(
        Any,
        type(
            "StubRun",
            (),
            {
                "id": "run-refl",
                "agent_id": "coder",
                "project_id": None,
                "session_id": fork.id,
                "run_kind": RunKind.MEMORY_REFLECTION,
                "status": RunStatus.RUNNING,
                "created_at": "2026-05-03T14:30:01+00:00",
                "iteration_count": 0,
                "events": [],
                "controls": lambda self: {
                    "compaction": "unavailable",
                    "background_tool_call_ids": [],
                },
            },
        )(),
    )

    with TestClient(app) as client:
        _override_bus_epoch(app.state.event_bus, epoch="epoch-abc")

        chat_runs: ChatRunManager = app.state.chat_runs
        active_snapshot = _attach_chat_runs_active_runs(chat_runs)
        active_snapshot.append(reflection_run)

        with client.websocket_connect("/ws") as websocket:
            hello = websocket.receive_json()

    assert hello["active_runs"] == [
        {
            "run_id": "run-refl",
            "agent_id": "coder",
            "project_id": None,
            "session_id": fork.id,
            "run_kind": "memory_reflection",
            "status": "running",
            "started_at": "2026-05-03T14:30:01+00:00",
            "iteration_count": 0,
            "controls": {"compaction": "unavailable", "background_tool_call_ids": []},
            "controls_sequence": 0,
            "sse_url": "/api/runs/run-refl/events",
            "source_session_id": source.id,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_mode", ["disconnect", "send_error", "send_cancel"])
async def test_shared_socket_closes_subscription_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_mode: str
) -> None:
    from types import SimpleNamespace

    import server.app as server_app

    bus = ServerEventBus()
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))
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
