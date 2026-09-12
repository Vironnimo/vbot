"""Server-push stream delivery, reconnect snapshots and window presence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from datetime import UTC, datetime
from typing import Any

from core.runs import RUN_AGENT_ACTIVITY_FIELD, RunStatus
from server._app_lifecycle import _app_chat_runs
from server._http_dependencies import HTTPException, Request, WebSocket
from server.events import (
    RESOURCE_KIND_CLIENTS,
    ServerEventBus,
)
from server.file_delivery import FileDelivery
from server.rpc.event_bridge import (
    publish_resource_changed,
    reflection_source_session_id,
)
from server.rpc.payloads import remove_opaque_provider_metadata

JsonObject = dict[str, Any]

SSE_HEARTBEAT_INTERVAL_SECONDS = 10.0

WS_HEARTBEAT_INTERVAL_SECONDS = 25.0

REPLAY_STATUS_FRESH = "fresh"

REPLAY_STATUS_RESUMED = "resumed"

REPLAY_STATUS_GAP = "gap"

REPLAY_STATUS_EPOCH_CHANGED = "epoch_changed"


async def _stream_websocket_events(websocket: WebSocket, stream: Any) -> None:
    stream_iter = stream.__aiter__()
    disconnect_task = asyncio.create_task(websocket.receive())
    # The pending stream read survives across loop iterations: cancelling it to
    # handle a stray client frame would finalize the async generator and
    # silently end server-push delivery.
    event_task: asyncio.Task[Any] | None = None
    try:
        while True:
            if event_task is None:
                event_task = asyncio.create_task(stream_iter.__anext__())
            done, _pending = await asyncio.wait(
                {event_task, disconnect_task},
                timeout=WS_HEARTBEAT_INTERVAL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await websocket.send_json(
                    {"type": "heartbeat", "timestamp": datetime.now(UTC).isoformat()}
                )
                continue

            if disconnect_task in done:
                message = disconnect_task.result()
                if message.get("type") == "websocket.disconnect":
                    return
                # Any other inbound frame is ignored; keep listening for the
                # disconnect without disturbing the pending log read.
                disconnect_task = asyncio.create_task(websocket.receive())

            if event_task in done:
                completed_event_task = event_task
                event_task = None
                try:
                    event = completed_event_task.result()
                except StopAsyncIteration:
                    return
                await websocket.send_json(event)
    finally:
        disconnect_task.cancel()
        with suppress(asyncio.CancelledError):
            await disconnect_task
        if event_task is not None:
            event_task.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await event_task


async def _close_log_stream(stream: Any) -> None:
    try:
        await asyncio.wait_for(stream.aclose(), timeout=1)
    except TimeoutError:
        return


def _parse_after_sequence(raw: str | None) -> int:
    """Parse the after_sequence query param, clamping to int ≥ 0 with 0 on failure."""
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return 0
    return max(value, 0)


def _parse_query_string(raw: str | None) -> str:
    """Return the query string value as-is, or empty when absent/blank."""
    if raw is None:
        return ""
    return raw.strip()


def _register_ws_client(websocket: WebSocket) -> Any:
    """Register the connecting window in the presence roster, if one is wired.

    Reads the client-minted connection id and accessor type from the query
    params and the browser/OS from the ``User-Agent`` header, then publishes a
    ``clients`` reload-on-change signal so other windows refresh the roster.
    Returns the registry entry (the unregister handle) or ``None`` when no
    registry exists (CLI-only runtime stub).
    """
    registry = getattr(websocket.app.state, "client_registry", None)
    if registry is None:
        return None
    entry = registry.register(
        connection_id=_parse_query_string(websocket.query_params.get("connection_id")),
        accessor=_parse_query_string(websocket.query_params.get("accessor")),
        user_agent=websocket.headers.get("user-agent", ""),
    )
    publish_resource_changed(websocket.app.state, RESOURCE_KIND_CLIENTS)
    return entry


def _unregister_ws_client(state: Any, entry: Any) -> None:
    """Remove a previously registered window and signal the roster change."""
    if entry is None:
        return
    registry = getattr(state, "client_registry", None)
    if registry is None:
        return
    registry.unregister(entry.id)
    publish_resource_changed(state, RESOURCE_KIND_CLIENTS)


def _bus_epoch(event_bus: ServerEventBus) -> str:
    """Return the event bus generation epoch."""
    return event_bus.epoch


def _bus_last_sequence(event_bus: ServerEventBus) -> int:
    """Return the bus's last issued sequence number."""
    return event_bus.last_sequence


def _connection_replay_status(
    event_bus: ServerEventBus,
    *,
    client_epoch: str,
    client_after_sequence: int,
    last_sequence: int,
) -> str:
    """Classify whether a reconnect cursor can be replayed without a gap."""
    server_epoch = _bus_epoch(event_bus)
    if client_epoch and client_epoch != server_epoch:
        return REPLAY_STATUS_EPOCH_CHANGED
    if not client_epoch or client_after_sequence <= 0:
        return REPLAY_STATUS_FRESH
    if client_after_sequence > last_sequence:
        return REPLAY_STATUS_GAP

    retained_events = event_bus.events
    if not retained_events or client_after_sequence == last_sequence:
        return REPLAY_STATUS_RESUMED
    oldest_retained_sequence = retained_events[0].get("sequence")
    if not isinstance(oldest_retained_sequence, int):
        return REPLAY_STATUS_GAP
    if client_after_sequence < oldest_retained_sequence - 1:
        return REPLAY_STATUS_GAP
    return REPLAY_STATUS_RESUMED


def _active_runs_snapshot(state: Any) -> list[JsonObject]:
    """Build the active-runs list for the connection_ready hello frame.

    Returns an empty list when the chat run manager is unavailable so the
    handshake can still complete — the snapshot is connection-specific and
    the client treats empty ``active_runs`` as authoritative for that scope.
    """
    try:
        chat_runs = _app_chat_runs(state)
    except HTTPException:
        return []
    snapshot: list[JsonObject] = []
    active_runs = getattr(chat_runs, "active_runs", None)
    if not callable(active_runs):
        return snapshot
    for run in active_runs():
        if run.status != RunStatus.RUNNING:
            continue
        item: JsonObject = {
            "run_id": run.id,
            "agent_id": run.agent_id,
            # Bare ``agent_id`` plus project so a reconnecting client can
            # rebuild the address-keyed session and re-attach the run.
            "project_id": run.project_id,
            "session_id": run.session_id,
            "run_kind": run.run_kind.value,
            "status": RunStatus.RUNNING.value,
            "started_at": run.created_at,
            "iteration_count": run.iteration_count,
            "controls": run.controls(),
            "controls_sequence": run.events[-1].sequence if run.events else 0,
            "sse_url": f"/api/runs/{run.id}/events",
        }
        if not getattr(run, "contributes_to_agent_activity", True):
            item[RUN_AGENT_ACTIVITY_FIELD] = False
        source_session_id = reflection_source_session_id(
            getattr(getattr(state, "runtime", None), "chat_sessions", None), run
        )
        if source_session_id:
            item["source_session_id"] = source_session_id
        snapshot.append(item)
    return snapshot


def _queues_snapshot(state: Any) -> list[JsonObject]:
    """Build the public Queue snapshot for the connection-ready hello frame."""
    try:
        chat_runs = _app_chat_runs(state)
    except HTTPException:
        return []
    all_queued = getattr(chat_runs, "all_queued", None)
    if not callable(all_queued):
        return []

    grouped: dict[tuple[str | None, str, str], list[JsonObject]] = {}
    for session_key, item in all_queued():
        if item.internal:
            continue
        grouped.setdefault(session_key, []).append(item.to_dict())

    return [
        {
            "project_id": project_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "items": grouped[(project_id, agent_id, session_id)],
        }
        for project_id, agent_id, session_id in sorted(
            grouped,
            key=lambda key: (key[0] or "", key[1], key[2]),
        )
    ]


def _replay_after_sequence(request: Request) -> int:
    if "after_sequence" in request.query_params:
        return _parse_after_sequence(request.query_params.get("after_sequence"))
    return _parse_after_sequence(request.headers.get("last-event-id"))


async def _sse_run_events(
    run: Any,
    *,
    after_sequence: int = 0,
    heartbeat_interval_seconds: float = SSE_HEARTBEAT_INTERVAL_SECONDS,
    file_delivery: FileDelivery | None = None,
) -> AsyncGenerator[str, None]:
    async with aclosing(run.subscribe(after_sequence=after_sequence)) as events:
        event_iterator = events.__aiter__()
        event_task: asyncio.Task[Any] | None = None
        try:
            while True:
                if event_task is None:
                    event_task = asyncio.create_task(anext(event_iterator))
                done, _pending = await asyncio.wait(
                    {event_task},
                    timeout=heartbeat_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    # A named transport-only event keeps quiet Tool calls from
                    # looking like a dead connection. It has no Run sequence and
                    # never enters timeline or replay state.
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue
                try:
                    event = event_task.result()
                except StopAsyncIteration:
                    break
                event_task = None
                data = remove_opaque_provider_metadata(
                    event.to_dict(),
                    file_delivery=file_delivery,
                )
                yield (
                    f"id: {event.sequence}\n"
                    f"event: {event.type}\n"
                    f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
                )
        finally:
            if event_task is not None and not event_task.done():
                event_task.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await event_task
