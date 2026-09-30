"""Server-push stream delivery, reconnect snapshots and window presence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing, suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.performance import count
from core.runs import RUN_AGENT_ACTIVITY_FIELD, RunStatus
from server._app_lifecycle import _app_chat_runs
from server._http_dependencies import Request, WebSocket
from server.clients import ClientEntry, ClientRegistry
from server.events import (
    RESOURCE_KIND_CLIENTS,
    ServerEventBus,
)
from server.file_delivery import FileDelivery
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.operations_methods import FILE_PREVIEW_WORKERS
from server.rpc.payloads import (
    file_url_candidates,
    remove_opaque_provider_metadata,
    verified_file_urls,
)

if TYPE_CHECKING:
    from core.sessions import SessionAddress

JsonObject = dict[str, Any]

SSE_HEARTBEAT_INTERVAL_SECONDS = 10.0

# Run events sent over SSE, all streams together; heartbeats are not counted.
SSE_EVENTS_METRIC = "events.sse"

WS_HEARTBEAT_INTERVAL_SECONDS = 25.0

REPLAY_STATUS_FRESH = "fresh"

REPLAY_STATUS_RESUMED = "resumed"

REPLAY_STATUS_GAP = "gap"

REPLAY_STATUS_EPOCH_CHANGED = "epoch_changed"


async def _stream_websocket_events(
    websocket: WebSocket, stream: Any, *, on_binary: Callable[[bytes], None] | None = None
) -> bool:
    """Push *stream* items to the socket: ``bytes`` as binary frames, others as JSON.

    Inbound binary frames go to *on_binary* when given; every other inbound
    frame is ignored. Returns ``True`` when *stream* ended and ``False`` when the
    client disconnected first.
    """
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
                    return False
                data = message.get("bytes")
                if on_binary is not None and isinstance(data, bytes):
                    on_binary(data)
                # Keep listening for the disconnect without disturbing the
                # pending stream read.
                disconnect_task = asyncio.create_task(websocket.receive())

            if event_task in done:
                completed_event_task = event_task
                event_task = None
                try:
                    event = completed_event_task.result()
                except StopAsyncIteration:
                    return True
                if isinstance(event, bytes):
                    await websocket.send_bytes(event)
                else:
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


def _register_ws_client(websocket: WebSocket) -> ClientEntry:
    """Register the connecting window in the presence roster.

    Reads the client-minted connection id and accessor type from the query
    params and the browser/OS from the ``User-Agent`` header, then publishes a
    ``clients`` reload-on-change signal so other windows refresh the roster.
    Returns the registry entry (the unregister handle).
    """
    registry: ClientRegistry = websocket.app.state.client_registry
    entry = registry.register(
        connection_id=_parse_query_string(websocket.query_params.get("connection_id")),
        accessor=_parse_query_string(websocket.query_params.get("accessor")),
        user_agent=websocket.headers.get("user-agent", ""),
    )
    publish_resource_changed(websocket.app.state, RESOURCE_KIND_CLIENTS)
    return entry


def _unregister_ws_client(state: Any, entry: ClientEntry) -> None:
    """Remove a previously registered window and signal the roster change."""
    state.client_registry.unregister(entry.id)
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

    The snapshot is connection-specific: the client treats an empty
    ``active_runs`` list as authoritative for that scope.
    """
    snapshot: list[JsonObject] = []
    for run in _app_chat_runs(state).active_runs():
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
            "controls_sequence": run.last_sequence,
            "sse_url": f"/api/runs/{run.id}/events",
        }
        if not getattr(run, "contributes_to_agent_activity", True):
            item[RUN_AGENT_ACTIVITY_FIELD] = False
        source_session_id = getattr(run, "source_session_id", None)
        if source_session_id:
            item["source_session_id"] = source_session_id
        snapshot.append(item)
    return snapshot


def _queues_snapshot(state: Any) -> list[JsonObject]:
    """Build the public Queue snapshot for the connection-ready hello frame."""
    grouped: dict[SessionAddress, list[JsonObject]] = {}
    for address, item in _app_chat_runs(state).all_queued():
        if item.internal:
            continue
        grouped.setdefault(address, []).append(item.to_dict())

    return [
        {
            "project_id": address.project_id,
            "agent_id": address.agent_id,
            "session_id": address.session_id,
            "items": items,
        }
        for address, items in sorted(
            grouped.items(),
            key=lambda entry: (
                entry[0].project_id or "",
                entry[0].agent_id,
                entry[0].session_id,
            ),
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
    include_file_urls: bool = False,
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
                if include_file_urls:
                    # Most events (every text delta) carry no file URL; only a
                    # candidate needs the filesystem verification off the loop.
                    candidates = file_url_candidates(data)
                    data["file_urls"] = (
                        await FILE_PREVIEW_WORKERS.run(
                            verified_file_urls, candidates, file_delivery
                        )
                        if candidates
                        else []
                    )
                count(SSE_EVENTS_METRIC)
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
