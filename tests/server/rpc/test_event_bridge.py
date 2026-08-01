"""Tests for bridging core lifecycle events into server event-bus payloads.

Focus: the queued-item bridge callback must not swallow a failed queued run
start silently — a non-cancellation failure logs at WARNING with a traceback,
while cancellation stays silent (mirrors the run-event bridge sibling).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.runs import (
    COMPACTION_ABORTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
    COMPACTION_STARTED_EVENT,
    MODEL_STEP_USAGE_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_STARTED_EVENT,
    TOOL_CALL_STDERR_EVENT,
    TOOL_CALL_STDOUT_EVENT,
    Run,
    RunEvent,
)
from server.events import ALLOWED_SERVER_EVENT_TYPES, ServerEventBus
from server.rpc import event_bridge
from server.rpc.event_bridge import (
    RUN_DELTA_EVENT_TYPES,
    SERVER_EVENT_TYPES,
    QueuedRunItem,
    _bridge_queued_item_to_event_bus,
    _publish_run_events,
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


def test_server_event_forwards_agent_activity_exclusion() -> None:
    event = RunEvent(
        sequence=1,
        run_id="run-system",
        agent_id="builder",
        session_id="sess-system",
        contributes_to_agent_activity=False,
        type=RUN_STARTED_EVENT,
    )

    summary = _server_event_from_run_event(event)

    assert summary["payload"]["contributes_to_agent_activity"] is False


def test_server_event_forwards_session_usage_on_terminal_events() -> None:
    """Terminal events carry the end-of-run session usage totals so clients can
    refresh their session-level token/cache display without re-fetching
    history."""
    session_usage = {"measured_turns": 3, "input_tokens": 1200, "cache_read_tokens": 900}
    context_usage = {"tokens": 1245, "estimated": False}
    event = RunEvent(
        sequence=9,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        type="run_completed",
        payload={
            "status": "completed",
            "session_usage": session_usage,
            "context_usage": context_usage,
        },
    )

    summary = _server_event_from_run_event(event)

    assert summary["payload"]["session_usage"] == session_usage
    assert summary["payload"]["context_usage"] == context_usage


def test_server_event_omits_session_usage_when_absent() -> None:
    event = RunEvent(
        sequence=9,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        type="run_completed",
        payload={"status": "completed"},
    )

    summary = _server_event_from_run_event(event)

    assert "session_usage" not in summary["payload"]


def test_server_event_forwards_failed_run_error() -> None:
    event = RunEvent(
        sequence=9,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        type=RUN_FAILED_EVENT,
        payload={"status": "failed", "error": "Provider request failed"},
    )

    summary = _server_event_from_run_event(event)

    assert summary["payload"]["error"] == "Provider request failed"


def test_server_event_forwards_model_step_usage_as_run_output() -> None:
    usage = {"input_tokens": 1200, "output_tokens": 45}
    session_usage = {
        "measured_turns": 3,
        "estimated_turns": 0,
        "input_tokens": 4200,
        "output_tokens": 160,
    }
    event = RunEvent(
        sequence=7,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        type=MODEL_STEP_USAGE_EVENT,
        payload={"usage": usage, "session_usage": session_usage},
    )

    summary = _server_event_from_run_event(event)

    assert summary["type"] == "run_output"
    assert summary["payload"]["output"] == {
        "usage": usage,
        "session_usage": session_usage,
    }


def test_server_event_forwards_compaction_checkpoint_as_run_output() -> None:
    checkpoint = {"id": "checkpoint-one", "summary": "Earlier work"}
    message = {"id": "message-one", "role": "compaction_checkpoint"}
    event = RunEvent(
        sequence=8,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        type=COMPACTION_COMPLETED_EVENT,
        payload={
            "message": message,
            "checkpoint": checkpoint,
            "checkpoint_id": "checkpoint-one",
            "history_available": True,
        },
    )

    summary = _server_event_from_run_event(event)

    assert summary["type"] == "run_output"
    assert summary["payload"]["output"] == event.payload


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (COMPACTION_STARTED_EVENT, {"context_tokens_before": 250_000}),
        (COMPACTION_ABORTED_EVENT, {"reason": "failed"}),
    ],
)
def test_server_event_forwards_compaction_lifecycle_output(
    event_type: str,
    payload: dict[str, Any],
) -> None:
    event = RunEvent(
        sequence=8,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        type=event_type,
        payload=payload,
    )

    summary = _server_event_from_run_event(event)

    assert summary["type"] == "run_output"
    assert summary["payload"]["output"] == payload


def test_server_event_hides_internal_continuation_on_terminal_events() -> None:
    continuation = {
        "checkpoint_id": "checkpoint-one",
        "cause": "network",
    }
    event = RunEvent(
        sequence=9,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        type="run_failed",
        payload={"status": "failed", "continuation": continuation},
    )

    summary = _server_event_from_run_event(event)

    assert "continuation" not in summary["payload"]


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


@pytest.mark.asyncio
async def test_terminal_run_event_invalidates_debug_traces_and_sessions() -> None:
    event_bus = ServerEventBus()
    run = Run(run_id="run-one", agent_id="agent-1", session_id="session-1")
    run.emit(RUN_STARTED_EVENT, {"status": "running"})
    run.emit(RUN_COMPLETED_EVENT, {"status": "completed"})

    await _publish_run_events(event_bus, run)

    assert [event["payload"] for event in event_bus.events[-2:]] == [
        {"kind": "debug_traces"},
        {"kind": "sessions", "scope": {"agent_id": "agent-1"}},
    ]


@pytest.mark.asyncio
async def test_excluded_run_still_invalidates_debug_traces_and_sessions() -> None:
    event_bus = ServerEventBus()
    run = Run(
        run_id="run-system",
        agent_id="agent-1",
        session_id="session-1",
        contributes_to_agent_activity=False,
    )
    run.emit(RUN_STARTED_EVENT, {"status": "running"})
    run.emit(RUN_COMPLETED_EVENT, {"status": "completed"})

    await _publish_run_events(event_bus, run)

    lifecycle_events = [event for event in event_bus.events if event["type"].startswith("run_")]
    assert lifecycle_events
    assert all(
        event["payload"]["contributes_to_agent_activity"] is False for event in lifecycle_events
    )
    assert [event["payload"] for event in event_bus.events[-2:]] == [
        {"kind": "debug_traces"},
        {"kind": "sessions", "scope": {"agent_id": "agent-1"}},
    ]


def test_process_output_deltas_are_sse_only_not_websocket_events() -> None:
    """Process stdout/stderr deltas stream over SSE and do not bridge to WebSocket."""
    process_delta_events = {TOOL_CALL_STDOUT_EVENT, TOOL_CALL_STDERR_EVENT}

    assert process_delta_events <= RUN_DELTA_EVENT_TYPES
    assert process_delta_events.isdisjoint(SERVER_EVENT_TYPES)
    assert process_delta_events.isdisjoint(ALLOWED_SERVER_EVENT_TYPES)


@pytest.mark.asyncio
async def test_run_event_bridge_observes_publish_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingEventBus:
        def publish(self, _event_type: str, _payload: dict[str, Any]) -> None:
            raise RuntimeError("publish failed")

    run = Run(run_id="run-one", agent_id="agent-1", session_id="session-1")
    run.emit(RUN_STARTED_EVENT, {"status": "running"})
    warnings: list[tuple[str, bool]] = []

    def record_warning(message: str, *args: Any, **kwargs: Any) -> None:
        warnings.append((message, kwargs.get("exc_info") is True))

    monkeypatch.setattr(event_bridge._LOGGER, "warning", record_warning)
    event_bridge._bridge_run_to_event_bus(SimpleNamespace(event_bus=FailingEventBus()), run)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert warnings == [("Run event bridge failed", True)]


def test_run_event_bridge_dedupe_cache_is_bounded() -> None:
    state = SimpleNamespace(
        run_event_bridge_run_ids=OrderedDict(),
        run_event_bridge_retention_limit=2,
    )
    cache = state.run_event_bridge_run_ids

    assert event_bridge._run_was_already_bridged(state, cache, "run-one") is False
    assert event_bridge._run_was_already_bridged(state, cache, "run-one") is True
    assert event_bridge._run_was_already_bridged(state, cache, "run-two") is False
    assert event_bridge._run_was_already_bridged(state, cache, "run-three") is False

    assert list(cache) == ["run-two", "run-three"]
    assert event_bridge._run_was_already_bridged(state, cache, "run-one") is False
    assert list(cache) == ["run-three", "run-one"]
