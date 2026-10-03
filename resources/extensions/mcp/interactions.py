"""Pending MCP user inputs (OAuth sign-ins, sampling approvals, elicitation), answered later."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import best_match

from core.utils.ids import new_id
from core.utils.timestamps import format_canonical_timestamp

# How long a server's request for user input waits for the user before it expires.
INPUT_REQUEST_TTL_SECONDS = 600.0
# Inputs a server requested; an expired one answers the server with ``cancel``. A
# sign-in bounds its own wait.
_SERVER_REQUESTS = frozenset({"elicitation", "sampling"})
_LOGGER = logging.getLogger("vbot.extensions.mcp")

# The string formats an elicitation form may request, checked strictly with the
# standard library; an answer to any other format is not checked.
_FORMATS = FormatChecker(formats=())
_FORMAT_EXPECTATIONS = {
    "email": "an email address such as name@example.com",
    "uri": "an absolute URI with a scheme, such as https://example.com/page",
    "date": "a date as YYYY-MM-DD, such as 2026-10-02",
    "date-time": "a date and time with a time zone (RFC 3339), such as 2026-10-02T14:30:00Z",
}
# The same rule as the WebUI form (`webui/src/lib/extensionInputs.js`).
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATE_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:(?P<second>[0-5][0-9]|60)(?:\.[0-9]+)?"
    r"(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


@_FORMATS.checks("email")
def _is_email(value: object) -> bool:
    return not isinstance(value, str) or _EMAIL.fullmatch(value) is not None


@_FORMATS.checks("uri")
def _is_uri(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if _SCHEME.match(value) is None or any(
        character.isspace() or not character.isprintable() for character in value
    ):
        return False
    try:
        urlsplit(value)
    except ValueError:
        return False
    return True


@_FORMATS.checks("date")
def _is_date(value: object) -> bool:
    return not isinstance(value, str) or (
        _DATE.fullmatch(value) is not None and _parses(date.fromisoformat, value)
    )


@_FORMATS.checks("date-time")
def _is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    match = _DATE_TIME.fullmatch(value)
    if match is None:
        return False
    # RFC 3339 allows a leap second, which datetime cannot represent.
    text = value.upper()
    if match["second"] == "60":
        text = text[: match.start("second")] + "59" + text[match.end("second") :]
    return _parses(datetime.fromisoformat, text)


def _parses(parse: Callable[[str], object], value: str) -> bool:
    try:
        parse(value)
    except ValueError:
        return False
    return True


def _schema_problem(schema: dict[str, Any], content: Any) -> str | None:
    """Why *content* does not satisfy *schema*, or ``None`` when it does."""
    error = best_match(Draft202012Validator(schema, format_checker=_FORMATS).iter_errors(content))
    if error is None:
        return None
    field = ".".join(str(part) for part in error.absolute_path)
    where = f"field '{field}'" if field else "the answer"
    if error.validator == "format":
        expected = _FORMAT_EXPECTATIONS.get(str(error.validator_value), error.validator_value)
        return f"{where} must be {expected}"
    if error.validator == "required":
        return str(error.message)
    return f"{where} does not meet the schema's '{error.validator}' rule"


@dataclass
class PendingInput:
    id: str
    connection: str
    kind: str
    payload: dict[str, Any]
    response: asyncio.Future[dict[str, Any]]
    session_id: str | None = None
    # When the input stops waiting for an answer (canonical UTC), if it does.
    expires_at: str | None = None


class InputRequests:
    """Pending inputs; ``on_change`` receives the id of each added or removed one.

    A server's request (elicitation, sampling approval) nobody answers within
    *ttl* seconds expires as cancelled.
    """

    def __init__(
        self,
        on_change: Callable[[str], None] | None = None,
        *,
        ttl: float = INPUT_REQUEST_TTL_SECONDS,
    ) -> None:
        self._pending: dict[str, PendingInput] = {}
        self._on_change = on_change
        self._ttl = ttl

    async def request(
        self,
        connection: str,
        kind: str,
        payload: dict[str, Any],
        session_id: str | None = None,
        *,
        expires_in: float | None = None,
    ) -> dict[str, Any]:
        """Wait for the user's answer to a new pending input.

        A server's request expires after the configured ttl; any other input
        waits until its caller stops, and *expires_in* states when that will be
        (a sign-in's own deadline), shown as ``expires_at``.
        """
        identifier = new_id("req", claim=lambda candidate: candidate not in self._pending)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        ttl = self._ttl if kind in _SERVER_REQUESTS else None
        lifetime = expires_in if ttl is None else ttl
        expires_at = (
            None
            if lifetime is None
            else format_canonical_timestamp(datetime.now(UTC) + timedelta(seconds=lifetime))
        )
        pending = PendingInput(
            identifier, connection, kind, copy.deepcopy(payload), future, session_id, expires_at
        )
        self._pending[identifier] = pending
        expiry = asyncio.timeout(ttl)
        try:
            self._changed(identifier)
            async with expiry:
                return await future
        except TimeoutError:
            if not expiry.expired():
                raise
            _LOGGER.info(
                "MCP input request expired unanswered (connection=%s kind=%s)", connection, kind
            )
            return {"action": "cancel"}
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
                "expires_at": item.expires_at,
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
            problem = (
                _schema_problem(schema, response.get("content"))
                if action == "accept" and schema is not None
                else None
            )
            if problem is not None:
                raise ValueError(
                    f"MCP input response does not satisfy the requested schema: {problem}"
                )
        if pending.kind == "oauth" and response.get("action") in {"decline", "cancel"}:
            pending.response.set_exception(ValueError("MCP sign-in was cancelled"))
        else:
            pending.response.set_result(copy.deepcopy(response))
        return {"id": identifier, "answered": True}

    def cancel_connection(self, connection: str) -> None:
        for pending in tuple(self._pending.values()):
            if pending.connection == connection and not pending.response.done():
                pending.response.cancel()
