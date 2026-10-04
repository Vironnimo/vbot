"""The operator's WebSocket to one Terminal Session.

The socket carries the Terminal's event stream (``terminal_ready`` snapshot,
then sequenced ``terminal_output``/``terminal_snapshot``/``terminal_state``)
to the viewer, and the viewer's requests to the Terminal:

- ``{"type": "input", "data": str}`` writes operator input; a failure answers
  ``{"type": "input_failed", "message": str}``.
- ``{"type": "resize", "request": int, "columns": int, "rows": int}`` resizes
  the PTY and answers ``resize_done`` (with the operator ``terminal`` summary)
  or ``resize_failed`` (with ``message``), naming the same ``request``.

Requests apply one at a time in arrival order, so keystrokes and sizes reach
the Terminal in the order the operator made them. Replies carry no sequence.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from typing import Any

from core.tools.terminal_manager import (
    TERMINAL_INPUT_MAX_CHARS,
    TerminalManager,
    TerminalManagerError,
)
from server._http_dependencies import WebSocket
from server._streams import _stream_websocket_events

JsonObject = dict[str, Any]


async def serve_terminal_socket(
    websocket: WebSocket, manager: TerminalManager, terminal_id: str
) -> None:
    """Stream one Terminal to an accepted socket and apply its requests.

    Raises ``TerminalNotFoundError`` before anything is streamed for an
    unknown Terminal.
    """
    requests: asyncio.Queue[JsonObject] = asyncio.Queue()
    replies: asyncio.Queue[JsonObject] = asyncio.Queue()
    worker = asyncio.create_task(
        _apply_requests(manager, terminal_id, requests, replies),
        name=f"terminal:{terminal_id}:socket-requests",
    )

    def receive(text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            message = None
        if isinstance(message, dict):
            requests.put_nowait(message)
        else:
            replies.put_nowait({"type": "protocol_error", "message": "Expected a JSON object"})

    try:
        events = manager.watch_for_operator(terminal_id)
        async with aclosing(_merge(events, replies)) as stream:
            await _stream_websocket_events(websocket, stream, on_text=receive)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


async def _apply_requests(
    manager: TerminalManager,
    terminal_id: str,
    requests: asyncio.Queue[JsonObject],
    replies: asyncio.Queue[JsonObject],
) -> None:
    while True:
        message = await requests.get()
        reply = await _apply(manager, terminal_id, message)
        if reply is not None:
            replies.put_nowait(reply)


async def _apply(
    manager: TerminalManager, terminal_id: str, message: JsonObject
) -> JsonObject | None:
    kind = message.get("type")
    if kind == "input":
        data = message.get("data")
        try:
            if not isinstance(data, str) or not data:
                raise ValueError("Terminal input must be a non-empty string")
            if len(data) > TERMINAL_INPUT_MAX_CHARS:
                raise ValueError(
                    f"Terminal input must not exceed {TERMINAL_INPUT_MAX_CHARS} characters"
                )
            await manager.send_operator_input(terminal_id, data)
        except (TerminalManagerError, ValueError) as error:
            return {"type": "input_failed", "message": str(error)}
        return None
    if kind == "resize":
        request = message.get("request")
        columns = message.get("columns")
        rows = message.get("rows")
        try:
            if not isinstance(columns, int) or not isinstance(rows, int):
                raise ValueError("columns and rows must be integers")
            if isinstance(columns, bool) or isinstance(rows, bool):
                raise ValueError("columns and rows must be integers")
            summary = await manager.resize_for_operator(terminal_id, columns=columns, rows=rows)
        except (TerminalManagerError, ValueError) as error:
            return {"type": "resize_failed", "request": request, "message": str(error)}
        return {"type": "resize_done", "request": request, "terminal": summary}
    return {"type": "protocol_error", "message": f"Unknown request type: {kind!r}"}


async def _merge(
    events: AsyncGenerator[JsonObject], replies: asyncio.Queue[JsonObject]
) -> AsyncGenerator[JsonObject]:
    """Yield Terminal events and request replies as they come; ends with the events."""
    pending_event: asyncio.Task[JsonObject] | None = None
    pending_reply: asyncio.Task[JsonObject] | None = None
    # The pending reads are cancelled before the event generator is closed:
    # closing it while a read still runs it would fail.
    async with aclosing(events):
        try:
            while True:
                if pending_event is None:
                    pending_event = asyncio.create_task(anext(events))
                if pending_reply is None:
                    pending_reply = asyncio.create_task(replies.get())
                done, _ = await asyncio.wait(
                    {pending_event, pending_reply}, return_when=asyncio.FIRST_COMPLETED
                )
                if pending_reply in done:
                    reply = pending_reply.result()
                    pending_reply = None
                    yield reply
                if pending_event in done:
                    finished_event, pending_event = pending_event, None
                    try:
                        event = finished_event.result()
                    except StopAsyncIteration:
                        return
                    yield event
        finally:
            for task in (pending_event, pending_reply):
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await task
