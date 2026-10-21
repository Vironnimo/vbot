"""Bridge core lifecycle events into server RPC/WebSocket payloads."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from contextlib import aclosing
from datetime import datetime
from typing import Any

from core.activity import FinishedWork, SessionActivity, SessionActivityState, WaitingFor
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    COMPACTION_ABORTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
    COMPACTION_STARTED_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
    GOAL_EVENT_RECORDED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    MODEL_STEP_USAGE_EVENT,
    PROVIDER_HEARTBEAT_EVENT,
    PROVIDER_REQUEST_STATUS_EVENT,
    REASONING_DELTA_EVENT,
    REASONING_EVENT,
    RUN_AGENT_ACTIVITY_FIELD,
    RUN_CANCELLED_EVENT,
    RUN_CHANGE_STATS_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_INTERRUPTED_EVENT,
    RUN_STARTED_EVENT,
    STREAM_ATTEMPT_RESTARTED_EVENT,
    SUBAGENT_MESSAGE_RECORDED_EVENT,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_OUTPUT_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    USER_MESSAGE_EVENT,
    WIDGET_STATE_RECORDED_EVENT,
    QueuedRunItem,
    Run,
    RunEvent,
    RunStatus,
)
from core.sessions import SessionAddress
from core.subagents import (
    SUBAGENT_SESSION_STARTED_EVENT,
    SUBAGENT_STATUS_CHANGED_EVENT,
)
from core.utils.timestamps import format_canonical_timestamp
from server.events import (
    ALLOWED_RESOURCE_KINDS,
    COMMAND_STATUS_CHANGED_EVENT,
    PROVIDER_AUTH_COMPLETED_EVENT,
    RECALL_INDEX_STATUS_EVENT,
    RESOURCE_CHANGED_EVENT,
    RESOURCE_KIND_DEBUG_TRACES,
    RESOURCE_KIND_SESSIONS,
    RUN_CANCELLED_SERVER_EVENT,
    RUN_COMPLETED_SERVER_EVENT,
    RUN_FAILED_SERVER_EVENT,
    RUN_INTERRUPTED_SERVER_EVENT,
    RUN_OUTPUT_SERVER_EVENT,
    RUN_STARTED_SERVER_EVENT,
    SESSION_ACTIVITY_CHANGED_EVENT,
)
from server.rpc.payloads import (
    remove_opaque_provider_metadata,
    remove_opaque_provider_metadata_async,
)

JsonObject = dict[str, Any]
_LOGGER = logging.getLogger("vbot.server.rpc.event_bridge")
DEFAULT_BRIDGED_RUN_RETENTION_LIMIT = 1024
# Bounds how long a Session activity change waits for the bridged Run events
# that precede it; a stalled bridge never holds activity back for longer.
SESSION_ACTIVITY_ORDER_TIMEOUT_SECONDS = 5.0

# Public payload field carrying the Session a background review fork reviews.
RUN_SOURCE_SESSION_FIELD = "source_session_id"


def _bridge_run_to_event_bus(state: Any, run: Run) -> None:
    if _run_was_already_bridged(state, state.run_event_bridge_run_ids, run.id):
        return
    task = asyncio.create_task(
        _publish_run_events(
            state.event_bus,
            run,
            file_delivery=state.file_delivery,
        ),
        name=f"run-event-bridge:{run.id}",
    )
    task.add_done_callback(_on_run_event_bridge_done)
    _track_run_event_bridge(state, run, task)


def _track_run_event_bridge(state: Any, run: Run, task: asyncio.Task[None]) -> None:
    """Keep each Session's running bridges until they end, for activity ordering."""
    bridges = getattr(state, "run_event_bridges", None)
    if bridges is None:
        return
    address = SessionAddress(run.project_id, run.agent_id, run.session_id)
    bridges.setdefault(address, {})[run.id] = (run, task)

    def forget(_task: asyncio.Task[None]) -> None:
        runs = bridges.get(address)
        if runs is not None:
            runs.pop(run.id, None)
            if not runs:
                bridges.pop(address, None)

    task.add_done_callback(forget)


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


def _run_was_already_bridged(
    state: Any, bridged_run_ids: OrderedDict[str, None], run_id: str
) -> bool:
    retention_limit = getattr(
        state,
        "run_event_bridge_retention_limit",
        DEFAULT_BRIDGED_RUN_RETENTION_LIMIT,
    )
    if not isinstance(retention_limit, int) or retention_limit < 1:
        retention_limit = DEFAULT_BRIDGED_RUN_RETENTION_LIMIT

    if run_id in bridged_run_ids:
        bridged_run_ids.move_to_end(run_id)
        return True
    bridged_run_ids[run_id] = None
    while len(bridged_run_ids) > retention_limit:
        bridged_run_ids.popitem(last=False)
    return False


def _bridge_queued_item_to_event_bus(state: Any, item: QueuedRunItem) -> None:
    """Bridge the eventual run start for one queued item into server lifecycle events."""

    def _on_run_started(future: asyncio.Future[Run]) -> None:
        if future.cancelled():
            return
        try:
            run = future.result()
        except asyncio.CancelledError:
            return
        except BaseException:
            _LOGGER.warning("Queued run bridge failed", exc_info=True)
            return
        _bridge_run_to_event_bus(state, run)

    item.future.add_done_callback(_on_run_started)


def _publish_provider_auth_completed_event(
    state: Any,
    *,
    provider_id: str,
    connection_id: str,
    account: str,
    success: bool,
) -> None:
    state.event_bus.publish(
        PROVIDER_AUTH_COMPLETED_EVENT,
        {
            "provider_id": provider_id,
            "connection_id": connection_id,
            "account": account,
            "success": success,
        },
    )


def publish_command_status_changed(
    state: Any, command_id: str, *, session_id: str, status: str, still_running: bool
) -> None:
    """Forward the new state of a background shell command to accessors.

    ``session_id`` names the Session the command belongs to; accessors show
    the status only there, since a fork shares its source's History but not
    its commands. ``status`` is ``running``, ``completed``, ``failed`` or
    ``stopped``, as in the ``background_command_statuses`` of ``chat.history``.
    ``still_running`` is true while processes the command started still run
    after its shell exited (``background_commands_still_running`` there); the
    user can stop them with ``chat.stop_command``.
    """
    state.event_bus.publish(
        COMMAND_STATUS_CHANGED_EVENT,
        {
            "command_id": command_id,
            "session_id": session_id,
            "status": status,
            "still_running": still_running,
        },
    )


def session_activity_service(state: Any) -> SessionActivity | None:
    """The Runtime's Session activity; None for a Runtime without one, or one stopping."""
    try:
        return state.runtime.session_activity  # type: ignore[no-any-return]
    except AttributeError, RuntimeError:
        return None


def waiting_for_payload(waiting_for: WaitingFor) -> JsonObject:
    return {
        "subagents": waiting_for.subagents,
        "commands": waiting_for.commands,
        "results": waiting_for.results,
    }


def session_activity_payload(
    address: SessionAddress,
    activity: SessionActivityState,
    finished: FinishedWork | None = None,
) -> JsonObject:
    """One Session's activity as accessors read it.

    ``state`` is ``working``, ``waiting`` or ``idle``; ``work_started_at`` is when
    the current work span began (canonical UTC, ``None`` while idle);
    ``waiting_for`` counts what a waiting Session waits for (all zero otherwise).
    ``contributes_to_agent_activity`` appears, as ``false``, only while every
    active and queued Run of a working Session is excluded from Agent activity.
    ``finished_work`` comes only with a move to ``idle``: the span it ended.
    """
    payload: JsonObject = {
        "project_id": address.project_id,
        "agent_id": address.agent_id,
        "session_id": address.session_id,
        "state": activity.state,
        "work_started_at": _timestamp(activity.work_started_at),
        "waiting_for": waiting_for_payload(activity.waiting_for),
    }
    if not activity.contributes_to_agent_activity:
        payload["contributes_to_agent_activity"] = False
    if finished is not None:
        payload["finished_work"] = {
            "started_at": format_canonical_timestamp(finished.started_at),
            "finished_at": format_canonical_timestamp(finished.finished_at),
            "duration_ms": finished.duration_ms,
        }
    return payload


def publish_session_activity_changed(
    state: Any,
    address: SessionAddress,
    activity: SessionActivityState,
    finished: FinishedWork | None = None,
) -> None:
    """Publish ``session_activity_changed`` after the Run events that caused it.

    A change waits until the bridges of this Session's finished Runs have
    published their terminal events (at most
    ``SESSION_ACTIVITY_ORDER_TIMEOUT_SECONDS``); changes of one Session keep
    their order. Without anything to wait for it is published at once.
    """
    payload = session_activity_payload(address, activity, finished)
    publications: dict[SessionAddress, asyncio.Task[None]] | None = getattr(
        state, "session_activity_publications", None
    )
    bridges = getattr(state, "run_event_bridges", None) or {}
    finishing = [
        task
        for run, task in bridges.get(address, {}).values()
        if run.status != RunStatus.RUNNING and not task.done()
    ]
    previous = publications.get(address) if publications is not None else None
    if previous is not None and previous.done():
        previous = None
    if publications is None or (previous is None and not finishing):
        state.event_bus.publish(SESSION_ACTIVITY_CHANGED_EVENT, payload)
        return

    async def publish_in_order() -> None:
        if previous is not None:
            await asyncio.wait({previous})
        if finishing:
            await asyncio.wait(finishing, timeout=SESSION_ACTIVITY_ORDER_TIMEOUT_SECONDS)
        state.event_bus.publish(SESSION_ACTIVITY_CHANGED_EVENT, payload)

    task = asyncio.create_task(
        publish_in_order(), name=f"session-activity-event:{address.agent_id}"
    )
    publications[address] = task

    def forget(done: asyncio.Task[None]) -> None:
        if publications.get(address) is done:
            publications.pop(address, None)
        if not done.cancelled() and done.exception() is not None:
            _LOGGER.warning("Session activity event failed", exc_info=done.exception())

    task.add_done_callback(forget)


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else format_canonical_timestamp(value)


def publish_recall_index_status(state: Any, status: Any) -> None:
    """Forward one semantic Recall index status to accessors.

    The payload is the ``recall.status`` result; it is accessor-only state and
    never reaches the Model.
    """
    state.event_bus.publish(RECALL_INDEX_STATUS_EVENT, dict(status.to_dict()))


def publish_resource_changed(
    state: Any,
    kind: str,
    *,
    scope: JsonObject | None = None,
) -> None:
    """Publish the generic "resource ``kind`` changed → reload it" signal.

    The event carries no payload beyond ``kind`` (and an optional ``scope`` that
    narrows it to one agent/session) — the client re-fetches the affected
    resource through its normal RPC, so this never ships data.
    """
    if kind not in ALLOWED_RESOURCE_KINDS:
        raise ValueError(f"unsupported resource kind: {kind}")
    payload: JsonObject = {"kind": kind}
    if scope:
        payload["scope"] = scope
    state.event_bus.publish(RESOURCE_CHANGED_EVENT, payload)


async def publish_archived_sessions(
    state: Any,
    addresses: Sequence[SessionAddress],
    *,
    landings: Mapping[tuple[str | None, str], str | None] | None = None,
) -> None:
    """Release archived Sessions in every window using each owner's own landing."""
    if not addresses:
        return

    def resolve_landings() -> dict[tuple[str | None, str], str | None]:
        resolved = dict(landings or {})
        for address in addresses:
            scope = (address.project_id, address.agent_id)
            if scope not in resolved:
                resolved[scope] = (
                    state.runtime.chat_sessions.newest_session_id(
                        address.agent_id, address.project_id
                    )
                    if address.project_id is not None
                    else state.runtime.agents.get(address.agent_id).current_session_id or None
                )
        return resolved

    resolved = await state.runtime.chat_sessions.run_async(resolve_landings)
    for address in addresses:
        publish_resource_changed(
            state,
            RESOURCE_KIND_SESSIONS,
            scope={
                "agent_id": address.agent_id,
                "project_id": address.project_id,
                "deleted_session_id": address.session_id,
                "next_session_id": resolved[(address.project_id, address.agent_id)],
            },
        )


def session_resource_scope(address: SessionAddress, **identifiers: str) -> JsonObject:
    """Return the ``sessions`` invalidation scope naming one exact Session.

    ``agent_id`` is bare and ``project_id`` is ``None`` for an identity Agent.
    Extra identifiers (a terminal ``run_id``, an acknowledged ``read_run_id``)
    let accessors apply what they already know instead of re-reading.
    """
    return {
        "project_id": address.project_id,
        "agent_id": address.agent_id,
        "session_id": address.session_id,
        **identifiers,
    }


def publish_session_changed(
    state: Any,
    address: SessionAddress,
    **identifiers: str,
) -> None:
    """Publish ``resource_changed(sessions)`` scoped to one exact Session."""
    publish_resource_changed(
        state,
        RESOURCE_KIND_SESSIONS,
        scope=session_resource_scope(address, **identifiers),
    )


async def _publish_run_events(
    event_bus: Any,
    run: Run,
    *,
    file_delivery: Any | None = None,
) -> None:
    # A review Run carries the Session it examines; stamping it needs no read.
    source_session_id = run.source_session_id or ""
    after_sequence = 0
    while True:
        previous_sequence = after_sequence
        latest_sequence = run.last_sequence
        async with aclosing(run.subscribe(after_sequence=after_sequence)) as events:
            async for event in events:
                if event.type in RUN_DELTA_EVENT_TYPES:
                    after_sequence = event.sequence
                    continue
                summary = await _server_event_from_run_event(
                    event,
                    file_delivery=file_delivery,
                    source_session_id=source_session_id,
                )
                event_bus.publish(summary["type"], summary["payload"])
                after_sequence = event.sequence
                if event.type not in RUN_TERMINAL_EVENT_TYPES:
                    continue
                event_bus.publish(
                    RESOURCE_CHANGED_EVENT,
                    {"kind": RESOURCE_KIND_DEBUG_TRACES},
                )
                # The terminal summary carries the completion facts; refresh
                # only the Session list rows this Run can affect.
                event_bus.publish(
                    RESOURCE_CHANGED_EVENT,
                    {
                        "kind": RESOURCE_KIND_SESSIONS,
                        "scope": session_resource_scope(
                            SessionAddress(run.project_id, run.agent_id, run.session_id),
                            run_id=run.id,
                        ),
                    },
                )
                return
        # File projection can lag behind deltas or terminal retention compaction.
        # Resume at the retained replay head after eviction, just as SSE does,
        # so the terminal summary and its invalidations are never abandoned.
        if run.last_sequence <= after_sequence or (
            after_sequence == previous_sequence and run.last_sequence <= latest_sequence
        ):
            _LOGGER.warning("Run event bridge ended without a terminal event: %s", run.id)
            return


async def _server_event_from_run_event(
    event: RunEvent,
    *,
    file_delivery: Any | None = None,
    source_session_id: str = "",
) -> JsonObject:
    payload: JsonObject = {
        "run_id": event.run_id,
        "agent_id": event.agent_id,
        # ``agent_id`` is bare; the project rides alongside so the client can
        # rebuild the ``agent@projekt`` address it keys session state by.
        "project_id": event.project_id,
        "session_id": event.session_id,
        "run_kind": event.run_kind.value,
        "run_event_type": event.type,
        "run_event_sequence": event.sequence,
        "run_event_timestamp": event.timestamp,
    }
    if source_session_id:
        payload[RUN_SOURCE_SESSION_FIELD] = source_session_id
    if not event.contributes_to_agent_activity:
        payload[RUN_AGENT_ACTIVITY_FIELD] = False
    if event.type in RUN_OUTPUT_EVENT_TYPES or event.type == RUN_STARTED_EVENT:
        payload["output"] = await remove_opaque_provider_metadata_async(
            event.payload,
            file_delivery=file_delivery,
        )
    if event.type in RUN_TERMINAL_EVENT_TYPES:
        payload["status"] = event.payload.get("status")
        if "timing" in event.payload:
            payload["timing"] = remove_opaque_provider_metadata(event.payload["timing"])
        if "session_usage" in event.payload:
            payload["session_usage"] = remove_opaque_provider_metadata(
                event.payload["session_usage"]
            )
        if "context_usage" in event.payload:
            payload["context_usage"] = remove_opaque_provider_metadata(
                event.payload["context_usage"]
            )
        if "change_stats" in event.payload:
            payload["change_stats"] = event.payload["change_stats"]
        # The Session recorded this completion as read: no unread marker.
        if event.payload.get("completion_read") is True:
            payload["completion_read"] = True
    if event.type == RUN_COMPLETED_EVENT and "usage" in event.payload:
        payload["usage"] = remove_opaque_provider_metadata(event.payload["usage"])
    if event.type == RUN_FAILED_EVENT and "error" in event.payload:
        payload["error"] = remove_opaque_provider_metadata(event.payload["error"])
    if event.type == RUN_INTERRUPTED_EVENT and "cause" in event.payload:
        payload["cause"] = remove_opaque_provider_metadata(event.payload["cause"])
    return {"type": SERVER_EVENT_TYPES.get(event.type, RUN_OUTPUT_SERVER_EVENT), "payload": payload}


RUN_OUTPUT_EVENT_TYPES = {
    "run_controls_changed",
    USER_MESSAGE_EVENT,
    REASONING_EVENT,
    TOOL_CALL_STARTED_EVENT,
    TOOL_CALL_RESULT_EVENT,
    SUBAGENT_SESSION_STARTED_EVENT,
    SUBAGENT_STATUS_CHANGED_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    COMPACTION_STARTED_EVENT,
    COMPACTION_ABORTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
    GOAL_EVENT_RECORDED_EVENT,
    SUBAGENT_MESSAGE_RECORDED_EVENT,
    WIDGET_STATE_RECORDED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    MODEL_STEP_USAGE_EVENT,
    PROVIDER_HEARTBEAT_EVENT,
    PROVIDER_REQUEST_STATUS_EVENT,
    RUN_CHANGE_STATS_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
}
RUN_DELTA_EVENT_TYPES = {
    "speech_playback",
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    STREAM_ATTEMPT_RESTARTED_EVENT,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_OUTPUT_EVENT,
}
RUN_TERMINAL_EVENT_TYPES = {
    RUN_COMPLETED_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_FAILED_EVENT,
    RUN_INTERRUPTED_EVENT,
}
SERVER_EVENT_TYPES = {
    RUN_STARTED_EVENT: RUN_STARTED_SERVER_EVENT,
    USER_MESSAGE_EVENT: RUN_OUTPUT_SERVER_EVENT,
    REASONING_EVENT: RUN_OUTPUT_SERVER_EVENT,
    TOOL_CALL_STARTED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    TOOL_CALL_RESULT_EVENT: RUN_OUTPUT_SERVER_EVENT,
    SUBAGENT_SESSION_STARTED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    SUBAGENT_STATUS_CHANGED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    ASSISTANT_OUTPUT_EVENT: RUN_OUTPUT_SERVER_EVENT,
    COMPACTION_STARTED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    COMPACTION_ABORTED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    COMPACTION_COMPLETED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    GOAL_EVENT_RECORDED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    SUBAGENT_MESSAGE_RECORDED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    WIDGET_STATE_RECORDED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    MODEL_STEP_USAGE_EVENT: RUN_OUTPUT_SERVER_EVENT,
    RUN_CHANGE_STATS_EVENT: RUN_OUTPUT_SERVER_EVENT,
    PROVIDER_HEARTBEAT_EVENT: RUN_OUTPUT_SERVER_EVENT,
    PROVIDER_REQUEST_STATUS_EVENT: RUN_OUTPUT_SERVER_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    RUN_COMPLETED_EVENT: RUN_COMPLETED_SERVER_EVENT,
    RUN_CANCELLED_EVENT: RUN_CANCELLED_SERVER_EVENT,
    RUN_FAILED_EVENT: RUN_FAILED_SERVER_EVENT,
    RUN_INTERRUPTED_EVENT: RUN_INTERRUPTED_SERVER_EVENT,
}
