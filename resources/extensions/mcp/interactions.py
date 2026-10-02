"""Pending MCP user inputs (OAuth sign-ins, sampling approvals, elicitation), answered later."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from core.utils.ids import new_id


@dataclass
class PendingInput:
    id: str
    connection: str
    kind: str
    payload: dict[str, Any]
    response: asyncio.Future[dict[str, Any]]
    session_id: str | None = None


class InputRequests:
    """Pending inputs; ``on_change`` receives the id of each added or removed one."""

    def __init__(self, on_change: Callable[[str], None] | None = None) -> None:
        self._pending: dict[str, PendingInput] = {}
        self._on_change = on_change

    async def request(
        self,
        connection: str,
        kind: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        identifier = new_id("req", claim=lambda candidate: candidate not in self._pending)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        pending = PendingInput(
            identifier, connection, kind, copy.deepcopy(payload), future, session_id
        )
        self._pending[identifier] = pending
        try:
            self._changed(identifier)
            return await future
        finally:
            self._pending.pop(identifier, None)
            self._changed(identifier)

    def _changed(self, identifier: str) -> None:
        if self._on_change is not None:
            self._on_change(identifier)

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
        if pending.kind in {"elicitation", "sampling"}:
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
