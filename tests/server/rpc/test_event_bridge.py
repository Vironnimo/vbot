"""Tests for bridging core lifecycle events into server event-bus payloads."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import ChatMessage
from core.chat.output_files import AssistantFileReference
from core.runs import (
    ASSISTANT_OUTPUT_EVENT,
    COMPACTION_ABORTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
    COMPACTION_STARTED_EVENT,
    MODEL_STEP_USAGE_EVENT,
    PROVIDER_REQUEST_STATUS_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_INTERRUPTED_EVENT,
    RUN_STARTED_EVENT,
    STREAM_ATTEMPT_RESTARTED_EVENT,
    TOOL_CALL_STDERR_EVENT,
    TOOL_CALL_STDOUT_EVENT,
    Run,
    RunEvent,
    RunKind,
)
from core.subagents import SUBAGENT_SESSION_STARTED_EVENT, SUBAGENT_STATUS_CHANGED_EVENT
from server.events import ALLOWED_SERVER_EVENT_TYPES, ServerEventBus
from server.file_delivery import FileDelivery
from server.rpc import event_bridge
from server.rpc.event_bridge import (
    RUN_DELTA_EVENT_TYPES,
    RUN_OUTPUT_EVENT_TYPES,
    RUN_SOURCE_SESSION_FIELD,
    SERVER_EVENT_TYPES,
    QueuedRunItem,
    _bridge_queued_item_to_event_bus,
    _publish_run_events,
    _server_event_from_run_event,
    publish_resource_changed,
)

JsonObject = dict[str, Any]

_TIMING = {
    "started_at": "2026-05-03T14:30:01+00:00",
    "completed_at": "2026-05-03T14:30:02+00:00",
    "duration_ms": 1000,
}


def _event(event_type: str, payload: JsonObject) -> RunEvent:
    return RunEvent(
        sequence=9,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        type=event_type,
        payload=payload,
    )


def _fail_start(future: asyncio.Future[Run]) -> None:
    future.set_exception(RuntimeError("queued start boom"))


def _cancel_start(future: asyncio.Future[Run]) -> None:
    future.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settle", "logged"),
    [
        pytest.param(_fail_start, True, id="failed-start"),
        pytest.param(_cancel_start, False, id="cancelled"),
    ],
)
async def test_queued_item_bridge_logs_only_a_failed_run_start(
    caplog: pytest.LogCaptureFixture,
    settle: Callable[[asyncio.Future[Run]], None],
    logged: bool,
) -> None:
    future: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
    _bridge_queued_item_to_event_bus(
        SimpleNamespace(event_bus=None), cast(QueuedRunItem, SimpleNamespace(future=future))
    )

    caplog.set_level(logging.WARNING, logger="vbot.server.rpc.event_bridge")
    settle(future)
    await asyncio.sleep(0)

    records = [record for record in caplog.records if record.name == "vbot.server.rpc.event_bridge"]
    # A failed start is never swallowed silently; cancellation stays silent.
    assert [record.exc_info is not None for record in records] == ([True] if logged else [])


@pytest.mark.parametrize(
    ("run_fields", "source_session_id", "attribution"),
    [
        pytest.param({}, "", {"project_id": None, "run_kind": "user"}, id="identity-run"),
        # The project rides beside the bare agent id so the client can rebuild the
        # ``agent@projekt`` address it keys a project Session by.
        pytest.param(
            {"project_id": "vbot"}, "", {"project_id": "vbot", "run_kind": "user"}, id="project-run"
        ),
        pytest.param(
            {"run_kind": RunKind.MEMORY_REFLECTION, "contributes_to_agent_activity": False},
            "source-uuid",
            {
                "project_id": None,
                "run_kind": "memory_reflection",
                "contributes_to_agent_activity": False,
                RUN_SOURCE_SESSION_FIELD: "source-uuid",
            },
            id="review-fork",
        ),
    ],
)
def test_run_started_event_carries_the_run_address_and_attribution(
    run_fields: JsonObject, source_session_id: str, attribution: JsonObject
) -> None:
    event = RunEvent(
        sequence=1,
        run_id="run-1",
        agent_id="builder",
        session_id="sess-uuid",
        type=RUN_STARTED_EVENT,
        payload={"status": "running", "queue_item_id": "qi-abc-123"},
        **run_fields,
    )

    summary = _server_event_from_run_event(event, source_session_id=source_session_id)

    assert summary == {
        "type": "run_started",
        "payload": {
            "run_id": "run-1",
            "agent_id": "builder",
            "session_id": "sess-uuid",
            "run_event_type": RUN_STARTED_EVENT,
            "run_event_sequence": 1,
            "run_event_timestamp": event.timestamp,
            # The queue item id lets the client drop the item it queued.
            "output": {"status": "running", "queue_item_id": "qi-abc-123"},
            **attribution,
        },
    }


_SESSION_USAGE = {"measured_turns": 3, "input_tokens": 1200, "cache_read_tokens": 900}
_CONTEXT_USAGE = {"tokens": 1245, "estimated": False}


@pytest.mark.parametrize(
    ("event_type", "event_payload", "projected", "withheld"),
    [
        pytest.param(
            RUN_COMPLETED_EVENT,
            {
                "status": "completed",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "estimated": True,
                    "reasoning_meta": {"secret": "opaque"},
                },
                "session_usage": _SESSION_USAGE,
                "context_usage": _CONTEXT_USAGE,
                "timing": _TIMING,
            },
            {
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 50, "estimated": True},
                "session_usage": _SESSION_USAGE,
                "context_usage": _CONTEXT_USAGE,
                "timing": _TIMING,
            },
            (),
            id="completed-with-usage",
        ),
        pytest.param(
            RUN_COMPLETED_EVENT,
            {"status": "completed"},
            {"status": "completed"},
            ("usage", "session_usage", "context_usage", "timing"),
            id="completed-without-usage",
        ),
        # Only a completed Run reports usage; internal continuation state stays hidden.
        pytest.param(
            RUN_FAILED_EVENT,
            {
                "status": "failed",
                "error": "Provider request failed",
                "usage": {"input_tokens": 10},
                "timing": _TIMING,
                "continuation": {"checkpoint_id": "checkpoint-one", "cause": "network"},
            },
            {"status": "failed", "error": "Provider request failed", "timing": _TIMING},
            ("usage", "continuation"),
            id="failed",
        ),
        pytest.param(
            RUN_INTERRUPTED_EVENT,
            {"status": "interrupted", "cause": "network"},
            {"status": "interrupted", "cause": "network"},
            ("error",),
            id="interrupted",
        ),
    ],
)
def test_terminal_events_project_only_their_completion_facts(
    event_type: str, event_payload: JsonObject, projected: JsonObject, withheld: tuple[str, ...]
) -> None:
    summary = _server_event_from_run_event(_event(event_type, event_payload))

    assert summary["type"] == event_type
    assert {key: summary["payload"][key] for key in projected} == projected
    assert set(withheld).isdisjoint(summary["payload"])


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        pytest.param(
            MODEL_STEP_USAGE_EVENT,
            {
                "usage": {"input_tokens": 1200, "output_tokens": 45},
                "session_usage": {"measured_turns": 3, "input_tokens": 4200},
            },
            id="model-step-usage",
        ),
        pytest.param(
            COMPACTION_STARTED_EVENT, {"context_tokens_before": 250_000}, id="compaction-started"
        ),
        pytest.param(
            COMPACTION_COMPLETED_EVENT,
            {
                "message": {"id": "message-one", "role": "compaction_checkpoint"},
                "checkpoint": {"id": "checkpoint-one", "summary": "Earlier work"},
                "checkpoint_id": "checkpoint-one",
                "history_available": True,
            },
            id="compaction-completed",
        ),
        pytest.param(COMPACTION_ABORTED_EVENT, {"reason": "failed"}, id="compaction-aborted"),
        # Provider retry progress reaches observers without becoming a failure.
        pytest.param(
            PROVIDER_REQUEST_STATUS_EVENT,
            {
                "state": "retrying",
                "model": "fixture/model",
                "attempt": 2,
                "max_attempts": 4,
                "error_kind": "timeout",
                "delay_seconds": 1.0,
            },
            id="provider-retry",
        ),
    ],
)
def test_progress_events_bridge_as_run_output(event_type: str, payload: JsonObject) -> None:
    summary = _server_event_from_run_event(_event(event_type, payload))

    assert summary["type"] == "run_output"
    assert summary["payload"]["run_event_type"] == event_type
    assert summary["payload"]["output"] == payload
    assert "error" not in summary["payload"]


def test_server_event_projects_assistant_file_reference(tmp_path: Path) -> None:
    image = tmp_path / "bridge.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    marker = f"file:{image}"
    message = ChatMessage.assistant(
        model="provider/model",
        content=marker,
        output_files=[
            AssistantFileReference(
                line_index=0, path=str(image.resolve()), start_index=0, end_index=len(marker)
            )
        ],
    )
    event = _event(ASSISTANT_OUTPUT_EVENT, {"message": message.to_dict()})

    summary = _server_event_from_run_event(
        event,
        file_delivery=FileDelivery(secret=b"bridge-secret"),
    )

    serialized = str(summary)
    assert "output_files" not in serialized
    assert str(image) not in serialized
    assert "![bridge.png](/api/files/" in summary["payload"]["output"]["message"]["content"]


@pytest.mark.parametrize(
    ("kind", "scope", "payload"),
    [
        # No data beyond the kind: the client re-fetches through its normal RPC.
        pytest.param("models", None, {"kind": "models"}, id="kind-only"),
        pytest.param(
            "queue",
            {"session_id": "s-1"},
            {"kind": "queue", "scope": {"session_id": "s-1"}},
            id="scoped",
        ),
    ],
)
def test_publish_resource_changed_signals_the_kind_and_its_scope(
    kind: str, scope: JsonObject | None, payload: JsonObject
) -> None:
    state = SimpleNamespace(event_bus=ServerEventBus())

    publish_resource_changed(state, kind, scope=scope)

    assert [(event["type"], event["payload"]) for event in state.event_bus.events] == [
        ("resource_changed", payload)
    ]


def test_publish_resource_changed_rejects_unknown_kind() -> None:
    state = SimpleNamespace(event_bus=ServerEventBus())

    with pytest.raises(ValueError):
        publish_resource_changed(state, "bogus")

    assert state.event_bus.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_fields", "terminal_type", "lifecycle_fields"),
    [
        pytest.param({}, RUN_COMPLETED_EVENT, {"project_id": None}, id="identity-run"),
        pytest.param(
            {"project_id": "vbot"}, RUN_FAILED_EVENT, {"project_id": "vbot"}, id="project-run"
        ),
        # A review fork is excluded from Agent activity yet still invalidates; its
        # source Session comes from the Run itself, without a Session read.
        pytest.param(
            {
                "run_kind": RunKind.MEMORY_REFLECTION,
                "contributes_to_agent_activity": False,
                "source_session_id": "source-uuid",
            },
            RUN_COMPLETED_EVENT,
            {"contributes_to_agent_activity": False, RUN_SOURCE_SESSION_FIELD: "source-uuid"},
            id="review-fork",
        ),
    ],
)
async def test_run_timeline_bridges_lifecycle_events_and_invalidates_its_exact_session(
    run_fields: JsonObject, terminal_type: str, lifecycle_fields: JsonObject
) -> None:
    event_bus = ServerEventBus()
    run = Run(run_id="run-one", agent_id="agent-1", session_id="session-1", **run_fields)
    run.emit(RUN_STARTED_EVENT, {"status": "running"})
    run.emit(TOOL_CALL_STDOUT_EVENT, {"tool_call_id": "call-1", "text": "streamed"})
    run.emit(terminal_type, {"status": terminal_type.removeprefix("run_")})

    await _publish_run_events(event_bus, run)

    lifecycle = [
        event["payload"] for event in event_bus.events if event["type"] != "resource_changed"
    ]
    # Process output deltas stream over SSE only.
    assert [payload["run_event_type"] for payload in lifecycle] == [
        RUN_STARTED_EVENT,
        terminal_type,
    ]
    assert all(payload.items() >= lifecycle_fields.items() for payload in lifecycle)
    assert [event["payload"] for event in event_bus.events[-2:]] == [
        {"kind": "debug_traces"},
        {
            "kind": "sessions",
            "scope": {
                "project_id": run_fields.get("project_id"),
                "agent_id": "agent-1",
                "session_id": "session-1",
                "run_id": "run-one",
            },
        },
    ]


def test_streaming_deltas_are_sse_only_and_subagent_lifecycle_reaches_websocket() -> None:
    """Deltas, including process output and stream restarts, stream over SSE only;
    subagent lifecycle events bridge to WebSocket as run output."""
    assert {
        TOOL_CALL_STDOUT_EVENT,
        TOOL_CALL_STDERR_EVENT,
        STREAM_ATTEMPT_RESTARTED_EVENT,
    } <= RUN_DELTA_EVENT_TYPES
    assert RUN_DELTA_EVENT_TYPES.isdisjoint(RUN_OUTPUT_EVENT_TYPES)
    assert RUN_DELTA_EVENT_TYPES.isdisjoint(SERVER_EVENT_TYPES)
    assert RUN_DELTA_EVENT_TYPES.isdisjoint(ALLOWED_SERVER_EVENT_TYPES)
    assert {SUBAGENT_SESSION_STARTED_EVENT, SUBAGENT_STATUS_CHANGED_EVENT} <= RUN_OUTPUT_EVENT_TYPES


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
    event_bridge._bridge_run_to_event_bus(
        SimpleNamespace(
            event_bus=FailingEventBus(),
            run_event_bridge_run_ids=OrderedDict(),
            file_delivery=FileDelivery(),
        ),
        run,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(warnings) == 1
    assert warnings[0][1] is True


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
