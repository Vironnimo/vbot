"""Bridge core lifecycle events into server RPC/WebSocket payloads."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any

from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    REASONING_DELTA_EVENT,
    REASONING_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_STARTED_EVENT,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    TOOL_CALL_STDERR_EVENT,
    TOOL_CALL_STDOUT_EVENT,
    USER_MESSAGE_EVENT,
    QueuedRunItem,
    Run,
    RunEvent,
)
from core.subagents import SUBAGENT_SESSION_STARTED_EVENT
from server.events import (
    PROVIDER_AUTH_COMPLETED_EVENT,
    RUN_CANCELLED_SERVER_EVENT,
    RUN_COMPLETED_SERVER_EVENT,
    RUN_FAILED_SERVER_EVENT,
    RUN_OUTPUT_SERVER_EVENT,
    RUN_STARTED_SERVER_EVENT,
)
from server.rpc.payloads import _remove_opaque_provider_metadata

JsonObject = dict[str, Any]
_LOGGER = logging.getLogger("vbot.server.rpc.event_bridge")
DEFAULT_BRIDGED_RUN_RETENTION_LIMIT = 1024


def _bridge_run_to_event_bus(state: Any, run: Run) -> None:
    event_bus = getattr(state, "event_bus", None)
    if event_bus is None:
        return
    bridged_run_ids = getattr(state, "run_event_bridge_run_ids", None)
    if _run_was_already_bridged(state, bridged_run_ids, run.id):
        return
    task = asyncio.create_task(_publish_run_events(event_bus, run))
    task.add_done_callback(_on_run_event_bridge_done)


def bridge_run_to_event_bus(state: Any, run: Run) -> None:
    """Bridge one Run timeline into the server WebSocket event bus."""
    _bridge_run_to_event_bus(state, run)


def _on_run_event_bridge_done(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        _LOGGER.warning("Run event bridge failed", exc_info=True)


def _run_was_already_bridged(state: Any, bridged_run_ids: Any, run_id: str) -> bool:
    retention_limit = getattr(
        state,
        "run_event_bridge_retention_limit",
        DEFAULT_BRIDGED_RUN_RETENTION_LIMIT,
    )
    if not isinstance(retention_limit, int) or retention_limit < 1:
        retention_limit = DEFAULT_BRIDGED_RUN_RETENTION_LIMIT

    if isinstance(bridged_run_ids, OrderedDict):
        if run_id in bridged_run_ids:
            bridged_run_ids.move_to_end(run_id)
            return True
        bridged_run_ids[run_id] = None
        while len(bridged_run_ids) > retention_limit:
            bridged_run_ids.popitem(last=False)
        return False

    if isinstance(bridged_run_ids, set):
        if run_id in bridged_run_ids:
            return True
        bridged_run_ids.add(run_id)
        while len(bridged_run_ids) > retention_limit:
            bridged_run_ids.remove(next(iter(bridged_run_ids)))
        return False

    return False


def _bridge_queued_item_to_event_bus(state: Any, item: QueuedRunItem) -> None:
    """Bridge the eventual run start for one queued item into server lifecycle events."""

    def _on_run_started(future: asyncio.Future[Run]) -> None:
        if future.cancelled():
            return
        try:
            run = future.result()
        except BaseException:
            return
        _bridge_run_to_event_bus(state, run)

    item.future.add_done_callback(_on_run_started)


def _publish_agent_event(state: Any, event_type: str, payload: JsonObject) -> None:
    """Publish an agent CRUD event to the server event bus if available."""
    event_bus = getattr(state, "event_bus", None)
    if event_bus is None:
        return
    event_bus.publish(event_type, payload)


def _publish_provider_auth_completed_event(
    state: Any,
    *,
    provider_id: str,
    connection_id: str,
    success: bool,
) -> None:
    event_bus = getattr(state, "event_bus", None)
    if event_bus is None:
        return
    event_bus.publish(
        PROVIDER_AUTH_COMPLETED_EVENT,
        {"provider_id": provider_id, "connection_id": connection_id, "success": success},
    )


async def _publish_run_events(event_bus: Any, run: Run) -> None:
    async for event in run.subscribe():
        if event.type in RUN_DELTA_EVENT_TYPES:
            continue
        summary = _server_event_from_run_event(event)
        event_bus.publish(summary["type"], summary["payload"])


def _server_event_from_run_event(event: RunEvent) -> JsonObject:
    payload: JsonObject = {
        "run_id": event.run_id,
        "agent_id": event.agent_id,
        "session_id": event.session_id,
        "run_event_type": event.type,
        "run_event_sequence": event.sequence,
        "run_event_timestamp": event.timestamp,
    }
    if event.type in RUN_OUTPUT_EVENT_TYPES:
        payload["output"] = _remove_opaque_provider_metadata(event.payload)
    if event.type in RUN_TERMINAL_EVENT_TYPES:
        payload["status"] = event.payload.get("status")
    if event.type == RUN_COMPLETED_EVENT and "usage" in event.payload:
        payload["usage"] = _remove_opaque_provider_metadata(event.payload["usage"])
    return {"type": SERVER_EVENT_TYPES.get(event.type, RUN_OUTPUT_SERVER_EVENT), "payload": payload}


RUN_OUTPUT_EVENT_TYPES = {
    USER_MESSAGE_EVENT,
    REASONING_EVENT,
    TOOL_CALL_STARTED_EVENT,
    TOOL_CALL_RESULT_EVENT,
    SUBAGENT_SESSION_STARTED_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
}
RUN_DELTA_EVENT_TYPES = {
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_STDOUT_EVENT,
    TOOL_CALL_STDERR_EVENT,
}
RUN_TERMINAL_EVENT_TYPES = {RUN_COMPLETED_EVENT, RUN_CANCELLED_EVENT, RUN_FAILED_EVENT}
SERVER_EVENT_TYPES = {
    RUN_STARTED_EVENT: RUN_STARTED_SERVER_EVENT,
    USER_MESSAGE_EVENT: RUN_OUTPUT_SERVER_EVENT,
    REASONING_EVENT: RUN_OUTPUT_SERVER_EVENT,
    TOOL_CALL_STARTED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    TOOL_CALL_RESULT_EVENT: RUN_OUTPUT_SERVER_EVENT,
    SUBAGENT_SESSION_STARTED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    ASSISTANT_OUTPUT_EVENT: RUN_OUTPUT_SERVER_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    RUN_COMPLETED_EVENT: RUN_COMPLETED_SERVER_EVENT,
    RUN_CANCELLED_EVENT: RUN_CANCELLED_SERVER_EVENT,
    RUN_FAILED_EVENT: RUN_FAILED_SERVER_EVENT,
}
