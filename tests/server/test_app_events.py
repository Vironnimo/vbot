"""Tests for app events."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.automation.cron import CronService
from core.runs import ChatRunManager, RunKind, RunStatus
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.format import write_bootstrap_marker
from server._app_lifecycle import (
    _register_bash_process_change_bridge,
    _register_cron_change_bridge,
    _register_run_event_bridge,
    _register_session_completion_read_bridge,
    _register_session_title_bridge,
)
from server._streams import (
    _active_runs_snapshot,
    _bus_epoch,
    _bus_last_sequence,
    _connection_replay_status,
    _parse_query_string,
    _queues_snapshot,
    _stream_websocket_events,
)
from server.app import (
    _is_reserved_server_path,
)
from server.events import ServerEventBus
from server.rpc.event_bridge import publish_bash_process_status_changed


@pytest.mark.asyncio
async def test_run_event_bridge_publishes_non_rpc_runs() -> None:
    chat_runs = ChatRunManager()
    state = type(
        "State",
        (),
        {
            "chat_runs": chat_runs,
            "event_bus": ServerEventBus(),
            "run_event_bridge_run_ids": set(),
        },
    )()
    unsubscribe = _register_run_event_bridge(state)

    async def execute(_run: Any) -> str:
        return "done"

    try:
        run = await chat_runs.start(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
            execute,
        )
        await run.wait()
        await _wait_for_events(state.event_bus, 4)
    finally:
        if callable(unsubscribe):
            unsubscribe()

    assert [event["type"] for event in state.event_bus.events] == [
        "run_started",
        "run_completed",
        "resource_changed",
        "resource_changed",
    ]
    assert all(event["payload"]["run_id"] == run.id for event in state.event_bus.events[:2])
    assert [event["payload"] for event in state.event_bus.events[-2:]] == [
        {"kind": "debug_traces"},
        {"kind": "sessions", "scope": {"agent_id": "coder"}},
    ]


def test_session_title_bridge_publishes_sessions_invalidation(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    state = type(
        "State",
        (),
        {
            "runtime": type("Runtime", (), {"chat_sessions": sessions})(),
            "event_bus": ServerEventBus(),
        },
    )()
    unsubscribe = _register_session_title_bridge(state)

    try:
        sessions.set_auto_title(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
            "Local title",
        )
    finally:
        if callable(unsubscribe):
            unsubscribe()

    assert state.event_bus.events[-1]["type"] == "resource_changed"
    assert state.event_bus.events[-1]["payload"] == {
        "kind": "sessions",
        "scope": {"agent_id": "coder"},
    }


def test_session_completion_read_bridge_publishes_sessions_invalidation(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    sessions.record_terminal_run(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        "run-one",
        "completed",
        "2026-07-20T10:00:00+00:00",
    )
    state = SimpleNamespace(
        runtime=SimpleNamespace(chat_sessions=sessions),
        event_bus=ServerEventBus(),
    )
    unsubscribe = _register_session_completion_read_bridge(state)

    try:
        sessions.mark_terminal_run_read(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one"), "run-one"
        )
    finally:
        if callable(unsubscribe):
            unsubscribe()

    assert state.event_bus.events[-1]["type"] == "resource_changed"
    assert state.event_bus.events[-1]["payload"] == {
        "kind": "sessions",
        "scope": {"agent_id": "coder"},
    }


def test_bash_process_change_bridge_publishes_terminal_notification() -> None:
    registered: list[Any] = []

    def add_terminal_callback(callback: Any) -> Any:
        registered.append(callback)
        return lambda: None

    state = SimpleNamespace(
        runtime=SimpleNamespace(
            process_manager=SimpleNamespace(add_terminal_callback=add_terminal_callback)
        ),
        event_bus=ServerEventBus(),
    )
    unsubscribe = _register_bash_process_change_bridge(state)

    try:
        registered[0]({"process_id": "process-one", "status": "completed"})
    finally:
        if callable(unsubscribe):
            unsubscribe()

    event = state.event_bus.events[-1]
    assert event["type"] == "bash_process_status_changed"
    assert event["payload"] == {"process_id": "process-one", "status": "completed"}


def test_bash_process_status_publisher_ignores_missing_bus_and_bad_payload() -> None:
    state = SimpleNamespace(event_bus=None)

    publish_bash_process_status_changed(state, {"process_id": "process-one"})
    publish_bash_process_status_changed(state, "not-a-dict")


def test_cron_change_bridge_publishes_scheduler_invalidation(tmp_path: Path) -> None:
    cron_service = CronService(cast(Any, SimpleNamespace()), tmp_path)
    state = SimpleNamespace(
        runtime=SimpleNamespace(cron_service=cron_service),
        event_bus=ServerEventBus(),
    )
    unsubscribe = _register_cron_change_bridge(state)

    try:
        cron_service.create_job(
            agent_id="coder",
            prompt="Scheduled work",
            schedule_type="cron",
            cron_expression="0 9 * * *",
        )
    finally:
        if callable(unsubscribe):
            unsubscribe()

    assert state.event_bus.events[-1]["type"] == "resource_changed"
    assert state.event_bus.events[-1]["payload"] == {"kind": "cron"}


async def _wait_for_events(event_bus: ServerEventBus, count: int) -> None:
    for _ in range(20):
        if len(event_bus.events) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected at least {count} events, got {len(event_bus.events)}")


# -- Unit tests for the /ws connection-ready handshake helpers --
def test_reserved_server_paths_include_websocket_and_control_prefixes() -> None:
    assert _is_reserved_server_path("health")
    assert _is_reserved_server_path("ws")
    assert _is_reserved_server_path("ws/logs")
    assert _is_reserved_server_path("ws/terminals/term-1")
    assert _is_reserved_server_path("api/rpc")
    assert not _is_reserved_server_path("chat")
    assert not _is_reserved_server_path("settings")


def test_parse_query_string_returns_blank_for_missing_or_whitespace() -> None:
    assert _parse_query_string(None) == ""
    assert _parse_query_string("") == ""
    assert _parse_query_string("   ") == ""
    assert _parse_query_string("  abc  ") == "abc"


def test_bus_epoch_returns_property_value_from_bus() -> None:
    bus = ServerEventBus()
    bus._epoch = "epoch-xyz"  # type: ignore[attr-defined]
    assert _bus_epoch(bus) == "epoch-xyz"


def test_bus_last_sequence_uses_property_value_from_bus() -> None:
    bus = ServerEventBus()
    bus.publish("run_started", {"id": "a"})
    bus.publish("run_output", {"id": "a"})
    assert _bus_last_sequence(bus) == 2


def test_bus_last_sequence_is_zero_for_empty_bus() -> None:
    bus = ServerEventBus()
    assert _bus_last_sequence(bus) == 0


def test_connection_replay_status_distinguishes_complete_and_incomplete_cursors() -> None:
    bus = ServerEventBus(event_retention_limit=2)
    bus._epoch = "epoch-current"  # type: ignore[attr-defined]
    for index in range(4):
        bus.publish("run_started", {"id": f"run-{index}"})

    assert (
        _connection_replay_status(
            bus,
            client_epoch="",
            client_after_sequence=0,
            last_sequence=bus.last_sequence,
        )
        == "fresh"
    )
    assert (
        _connection_replay_status(
            bus,
            client_epoch="epoch-current",
            client_after_sequence=2,
            last_sequence=bus.last_sequence,
        )
        == "resumed"
    )
    assert (
        _connection_replay_status(
            bus,
            client_epoch="epoch-current",
            client_after_sequence=1,
            last_sequence=bus.last_sequence,
        )
        == "gap"
    )
    assert (
        _connection_replay_status(
            bus,
            client_epoch="epoch-current",
            client_after_sequence=5,
            last_sequence=bus.last_sequence,
        )
        == "gap"
    )
    assert (
        _connection_replay_status(
            bus,
            client_epoch="epoch-old",
            client_after_sequence=2,
            last_sequence=bus.last_sequence,
        )
        == "epoch_changed"
    )


def test_active_runs_snapshot_includes_only_running_runs_with_sse_url(
    tmp_path: Path,
) -> None:
    chat_runs = ChatRunManager()
    snapshot: list[Any] = []
    chat_runs.active_runs = lambda: list(snapshot)  # type: ignore[method-assign]

    running_run = cast(
        Any,
        type(
            "StubRun",
            (),
            {
                "id": "run-running",
                "agent_id": "coder",
                "project_id": "acme",
                "session_id": "session-running",
                "run_kind": RunKind.USER,
                "status": RunStatus.RUNNING,
                "created_at": "2026-08-05T18:00:00+00:00",
                "iteration_count": 4,
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
                "project_id": "acme",
                "session_id": "session-terminal",
                "run_kind": RunKind.USER,
                "status": RunStatus.COMPLETED,
                "created_at": "2026-08-05T17:00:00+00:00",
                "iteration_count": 2,
                "events": [],
                "controls": lambda self: {
                    "compaction": "unavailable",
                    "background_tool_call_ids": [],
                },
            },
        )(),
    )
    snapshot.extend([running_run, terminal_run])

    state = type("State", (), {"chat_runs": chat_runs})()
    result = _active_runs_snapshot(state)

    # The project rides alongside the bare agent id so a reconnecting client can
    # rebuild the address-keyed session and re-attach the run.
    assert result == [
        {
            "run_id": "run-running",
            "agent_id": "coder",
            "project_id": "acme",
            "session_id": "session-running",
            "run_kind": "user",
            "status": "running",
            "started_at": "2026-08-05T18:00:00+00:00",
            "iteration_count": 4,
            "controls": {"compaction": "unavailable", "background_tool_call_ids": []},
            "controls_sequence": 0,
            "sse_url": "/api/runs/run-running/events",
        }
    ]


class _ScriptedLogWebSocket:
    """Minimal /ws/logs websocket double: scripted inbound frames, recorded sends."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send_json(self, event: dict[str, Any]) -> None:
        self.sent.append(event)


def _queue_log_stream(queue: asyncio.Queue[Any]) -> Any:
    """A real async generator, so a cancelled __anext__ finalizes it like the live tail."""

    async def generate() -> Any:
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item

    return generate()


@pytest.mark.asyncio
async def test_stream_websocket_events_survives_stray_client_frames() -> None:
    # Arrange
    websocket = _ScriptedLogWebSocket()
    log_events: asyncio.Queue[Any] = asyncio.Queue()
    streamer = asyncio.create_task(
        _stream_websocket_events(cast(Any, websocket), _queue_log_stream(log_events))
    )

    # Act: a stray client frame arrives while no log event is pending; a log
    # event afterwards must still be delivered instead of the tail dying.
    await websocket.incoming.put({"type": "websocket.receive", "text": "keepalive"})
    await log_events.put({"line": "hello"})

    async def wait_for_delivery() -> None:
        while not websocket.sent:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_delivery(), timeout=1)
    await websocket.incoming.put({"type": "websocket.disconnect"})
    await asyncio.wait_for(streamer, timeout=1)

    # Assert
    assert websocket.sent == [{"line": "hello"}]


def test_active_runs_snapshot_keeps_project_id_none_for_identity_run(
    tmp_path: Path,
) -> None:
    chat_runs = ChatRunManager()
    snapshot: list[Any] = []
    chat_runs.active_runs = lambda: list(snapshot)  # type: ignore[method-assign]

    identity_run = cast(
        Any,
        type(
            "StubRun",
            (),
            {
                "id": "run-identity",
                "agent_id": "coder",
                "project_id": None,
                "session_id": "session-identity",
                "run_kind": RunKind.USER,
                "status": RunStatus.RUNNING,
                "created_at": "2026-08-05T18:00:00+00:00",
                "iteration_count": 1,
                "events": [],
                "controls": lambda self: {
                    "compaction": "unavailable",
                    "background_tool_call_ids": [],
                },
            },
        )(),
    )
    snapshot.append(identity_run)

    state = type("State", (), {"chat_runs": chat_runs})()
    result = _active_runs_snapshot(state)

    assert result[0]["project_id"] is None


def test_active_runs_snapshot_marks_runs_excluded_from_agent_activity() -> None:
    chat_runs = ChatRunManager()
    hidden_run = cast(
        Any,
        type(
            "StubRun",
            (),
            {
                "id": "run-system",
                "agent_id": "coder",
                "project_id": None,
                "session_id": "session-system",
                "run_kind": RunKind.SYSTEM,
                "status": RunStatus.RUNNING,
                "created_at": "2026-08-05T18:00:00+00:00",
                "iteration_count": 0,
                "events": [],
                "controls": lambda self: {
                    "compaction": "unavailable",
                    "background_tool_call_ids": [],
                },
                "contributes_to_agent_activity": False,
            },
        )(),
    )
    chat_runs.active_runs = lambda: [hidden_run]  # type: ignore[method-assign]

    result = _active_runs_snapshot(type("State", (), {"chat_runs": chat_runs})())

    assert result[0]["contributes_to_agent_activity"] is False


def test_active_runs_snapshot_returns_empty_list_when_run_manager_missing() -> None:
    state = type("State", (), {})()
    result = _active_runs_snapshot(state)

    assert result == []


def test_active_runs_snapshot_returns_empty_list_when_manager_lacks_accessor() -> None:
    chat_runs = ChatRunManager()
    state = type("State", (), {"chat_runs": chat_runs})()
    result = _active_runs_snapshot(state)

    assert result == []


def test_queues_snapshot_groups_public_items_by_sorted_session_scope() -> None:
    class StubItem:
        def __init__(self, item_id: str, *, internal: bool = False) -> None:
            self.item_id = item_id
            self.internal = internal

        def to_dict(self) -> dict[str, Any]:
            return {"id": self.item_id, "internal": self.internal}

    queued = [
        (("project-b", "writer", "session-b"), StubItem("second")),
        ((None, "coder", "session-a"), StubItem("first")),
        ((None, "coder", "session-a"), StubItem("hidden", internal=True)),
        (("project-b", "writer", "session-b"), StubItem("third")),
    ]
    chat_runs = ChatRunManager()
    chat_runs.all_queued = lambda: cast(Any, list(queued))  # type: ignore[method-assign]
    state = type("State", (), {"chat_runs": chat_runs})()

    assert _queues_snapshot(state) == [
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
