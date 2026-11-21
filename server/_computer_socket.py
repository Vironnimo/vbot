"""Native Desktop connection: bounded requests, exact identity and no mutation replay."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from core.computers import ComputerError, ComputerRegistry
from core.computers.computers import MAX_FRAME_BYTES, ComputerConnection
from core.utils.workers import cancel_and_wait, settle_before_cancelling
from server._http_dependencies import WebSocketDisconnect
from server.access import scope_principal


async def serve_computer_socket(websocket: Any, computers: ComputerRegistry) -> None:
    """Only native clients register; the existing access guard owns server admission."""
    principal = scope_principal(websocket.scope)
    if principal is None or websocket.headers.get("origin"):
        await websocket.close(code=4403, reason="Native Desktop connection required")
        return
    await websocket.accept()
    try:
        async with asyncio.timeout(10):
            hello = await _receive(websocket)
        if not isinstance(hello, dict):
            raise ComputerError("computer_protocol", "Computer registration must be an object.")
        connection = await computers.connect(hello, actor=principal.actor)
        link = asyncio.create_task(_exchange(websocket, computers, connection))
        ended = asyncio.create_task(connection.ended.wait())
        try:
            done, _ = await asyncio.wait((link, ended), return_when=asyncio.FIRST_COMPLETED)
            if link in done:
                await link
            close_requested = connection.closed
        finally:
            # Publish loss before waiting for the old link's I/O to unwind.
            computers.disconnect(connection)
            # A replaced link can be blocked even in its welcome or a send.
            link.cancel()
            ended.cancel()
            await settle_before_cancelling(asyncio.gather(link, ended, return_exceptions=True))
        if close_requested:
            await websocket.close(code=1000, reason="Computer connection closed")
    except ComputerError as error:
        await websocket.send_json(
            {"type": "error", "error": {"code": error.code, "message": error.message}}
        )
        await websocket.close(code=4403 if error.code == "computer_identity_conflict" else 1008)
    except TimeoutError, ValueError, json.JSONDecodeError:
        await websocket.close(code=1008, reason="Invalid computer connection message")


async def _exchange(
    websocket: Any, computers: ComputerRegistry, connection: ComputerConnection
) -> None:
    await websocket.send_json(
        {
            "type": "welcome",
            "protocol": 1,
            "computer_id": connection.info.id,
            "instance_id": connection.info.instance_id,
            "server_instance": computers.server_instance,
            "capability_updates": 1,
        }
    )
    sender = asyncio.create_task(
        _send(websocket, connection), name=f"computer-link:{connection.info.id}"
    )
    receiver = asyncio.create_task(_receive_messages(websocket, computers, connection))
    try:
        done, _ = await asyncio.wait((receiver, sender), return_when=asyncio.FIRST_COMPLETED)
        await (sender if sender in done else receiver)
    finally:
        await cancel_and_wait(sender, receiver)


async def _receive_messages(
    websocket: Any, computers: ComputerRegistry, connection: ComputerConnection
) -> None:
    while not connection.closed:
        computers.receive(connection, await _receive(websocket))


async def _receive(websocket: Any) -> Any:
    frame = await websocket.receive()
    if frame["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(frame.get("code", 1000), frame.get("reason", ""))
    raw = frame.get("text")
    if not isinstance(raw, str):
        raise ValueError("Expected a text frame")
    # Valid WebSocket text uses at most four UTF-8 bytes per code point.
    length = len(raw)
    if length > MAX_FRAME_BYTES or (
        length > MAX_FRAME_BYTES // 4
        and not raw.isascii()
        and len(raw.encode("utf-8")) > MAX_FRAME_BYTES
    ):
        raise ValueError("Computer frame too large")
    return json.loads(raw)


async def _send(websocket: Any, connection: ComputerConnection) -> None:
    while not connection.closed:
        message = await connection.outgoing.get()
        if message is None:
            return
        future = connection.pending.get(message["id"])
        if future is None or future.cancelled():
            continue
        await websocket.send_json(message)
