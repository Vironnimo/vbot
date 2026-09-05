"""Resumable MCP user input and OAuth without blocking a management command."""

from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator


@dataclass
class PendingInput:
    id: str
    connection: str
    kind: str
    payload: dict[str, Any]
    response: asyncio.Future[dict[str, Any]]
    session_id: str | None = None


class InputRequests:
    def __init__(self) -> None:
        self._pending: dict[str, PendingInput] = {}

    async def request(
        self,
        connection: str,
        kind: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        identifier = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        pending = PendingInput(
            identifier, connection, kind, copy.deepcopy(payload), future, session_id
        )
        self._pending[identifier] = pending
        try:
            return await future
        finally:
            self._pending.pop(identifier, None)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "connection": item.connection,
                "kind": item.kind,
                "payload": copy.deepcopy(item.payload),
                "session_id": item.session_id,
            }
            for item in self._pending.values()
        ]

    def respond(self, identifier: str, response: dict[str, Any]) -> dict[str, Any]:
        pending = self._pending.get(identifier)
        if pending is None or pending.response.done():
            raise ValueError("MCP input request no longer exists")
        if pending.kind == "elicitation":
            action = response.get("action")
            if action not in {"accept", "decline", "cancel"}:
                raise ValueError("MCP input response requires accept, decline, or cancel")
            schema = pending.payload.get("requestedSchema")
            if (
                action == "accept"
                and schema is not None
                and not Draft202012Validator(schema).is_valid(response.get("content"))
            ):
                raise ValueError("MCP input response does not satisfy the requested schema")
        if pending.kind == "oauth" and response.get("action") in {"decline", "cancel"}:
            pending.response.set_exception(ValueError("MCP sign-in was cancelled"))
        else:
            pending.response.set_result(copy.deepcopy(response))
        return {"id": identifier, "answered": True}

    def cancel_connection(self, connection: str) -> None:
        for pending in tuple(self._pending.values()):
            if pending.connection == connection and not pending.response.done():
                pending.response.cancel()
