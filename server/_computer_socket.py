"""Native Desktop connection: bounded requests, exact identity and no mutation replay."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from core.computers import ComputerError, ComputerRegistry
from core.computers.computers import MAX_FRAME_BYTES, ComputerConnection
from core.utils.workers import cancel_and_wait
from server.access import scope_principal


async def serve_computer_socket(websocket: Any, computers: ComputerRegistry) -> None:
    """Only native clients register; the existing access guard owns server admission."""
    principal = scope_principal(websocket.scope)
    if principal is None or websocket.headers.get("origin"):
        await websocket.close(code=4403, reason="Native Desktop connection required")
        return
    await websocket.accept()
    connection: ComputerConnection | None = None
    sender: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(10):
            hello = await _receive(websocket)
        if not isinstance(hello, dict):
            raise ComputerError("computer_protocol", "Computer registration must be an object.")
        connection = await computers.connect(hello, actor=principal.actor)
        await websocket.send_json(
            {
                "type": "welcome",
                "protocol": 1,
                "computer_id": connection.info.id,
                "instance_id": connection.info.instance_id,
                "server_instance": computers.server_instance,
            }
        )
        computer_id = connection.info.id
        sender = asyncio.create_task(
            _send(websocket, connection), name=f"computer-link:{computer_id}"
        )
        while not connection.closed:
            incoming = asyncio.create_task(_receive(websocket))
            try:
                done, _ = await asyncio.wait(
                    (incoming, sender), return_when=asyncio.FIRST_COMPLETED
                )
                if sender in done:
                    await sender
                    break
                computers.receive(connection, incoming.result())
            finally:
                await cancel_and_wait(incoming)
        if connection.closed:
            # A newer link of the same installation replaced this one.
            await websocket.close(code=1000, reason="Computer connection replaced")
    except ComputerError as error:
        await websocket.send_json(
            {"type": "error", "error": {"code": error.code, "message": error.message}}
        )
        await websocket.close(code=4403 if error.code == "computer_identity_conflict" else 1008)
    except TimeoutError, ValueError, json.JSONDecodeError:
        await websocket.close(code=1008, reason="Invalid computer connection message")
    finally:
        if connection is not None:
            computers.disconnect(connection)
        if sender is not None:
            await cancel_and_wait(sender)


async def _receive(websocket: Any) -> Any:
    raw = await websocket.receive_text()
    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
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
