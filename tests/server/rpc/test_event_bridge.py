"""Tests for bridging core lifecycle events into server event-bus payloads.

Focus: the queued-item bridge callback must not swallow a failed queued run
start silently — a non-cancellation failure logs at WARNING with a traceback,
while cancellation stays silent (mirrors the run-event bridge sibling).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import cast

import pytest

from core.runs import RUN_STARTED_EVENT, RunEvent
from server.events import ServerEventBus
from server.rpc.event_bridge import (
    QueuedRunItem,
    _bridge_queued_item_to_event_bus,
    _server_event_from_run_event,
    publish_resource_changed,
)


@pytest.mark.asyncio
async def test_queued_item_bridge_logs_when_run_start_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = SimpleNamespace(event_bus=None)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    item = SimpleNamespace(future=future)

    _bridge_queued_item_to_event_bus(state, cast(QueuedRunItem, item))

    caplog.set_level(logging.WARNING, logger="vbot.server.rpc.event_bridge")
    future.set_exception(RuntimeError("queued start boom"))
    await asyncio.sleep(0)

    failure_records = [
        record
        for record in caplog.records
        if record.name == "vbot.server.rpc.event_bridge"
        and "Queued run bridge failed" in record.getMessage()
    ]
    assert len(failure_records) == 1
    assert failure_records[0].exc_info is not None


@pytest.mark.asyncio
async def test_queued_item_bridge_silent_on_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = SimpleNamespace(event_bus=None)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    item = SimpleNamespace(future=future)

    _bridge_queued_item_to_event_bus(state, cast(QueuedRunItem, item))

    caplog.set_level(logging.WARNING, logger="vbot.server.rpc.event_bridge")
    future.cancel()
    await asyncio.sleep(0)

    assert [
        record for record in caplog.records if record.name == "vbot.server.rpc.event_bridge"
    ] == []


def test_server_event_carries_project_id_for_project_run() -> None:
    """The bridged WS payload carries the run's project alongside the bare agent
    id, so the client can rebuild the ``agent@projekt`` address it keys a
    project-agent session by and re-attach through the backstop."""
    event = RunEvent(
        sequence=1,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        project_id="vbot",
        type=RUN_STARTED_EVENT,
    )

    summary = _server_event_from_run_event(event)

    assert summary["payload"]["agent_id"] == "builder"
    assert summary["payload"]["project_id"] == "vbot"


def test_server_event_keeps_project_id_none_for_identity_run() -> None:
    """An identity run's bridged payload keeps project_id None — the client's
    address rebuild then yields the bare id, byte-identical to today."""
    event = RunEvent(
        sequence=1,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        type=RUN_STARTED_EVENT,
    )

    summary = _server_event_from_run_event(event)

    assert summary["payload"]["project_id"] is None


def test_publish_resource_changed_emits_kind_only_payload() -> None:
    state = SimpleNamespace(event_bus=ServerEventBus())

    publish_resource_changed(state, "models")

    assert len(state.event_bus.events) == 1
    event = state.event_bus.events[0]
    assert event["type"] == "resource_changed"
    # No data beyond the kind — the client re-fetches through its normal RPC.
    assert event["payload"] == {"kind": "models"}


def test_publish_resource_changed_includes_scope_when_given() -> None:
    state = SimpleNamespace(event_bus=ServerEventBus())

    publish_resource_changed(state, "queue", scope={"session_id": "s-1"})

    assert state.event_bus.events[-1]["payload"] == {
        "kind": "queue",
        "scope": {"session_id": "s-1"},
    }


def test_publish_resource_changed_is_noop_without_event_bus() -> None:
    state = SimpleNamespace(event_bus=None)

    # A CLI-only runtime stub has no bus — the helper must no-op, not crash.
    publish_resource_changed(state, "models")


def test_publish_resource_changed_rejects_unknown_kind() -> None:
    state = SimpleNamespace(event_bus=ServerEventBus())

    with pytest.raises(ValueError, match="unsupported resource kind"):
        publish_resource_changed(state, "bogus")

    assert state.event_bus.events == []
