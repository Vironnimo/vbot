"""History request validation and snapshot-bound continuation cursors."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from core.sessions import (
    SessionHistoryCheckpoint,
)
from core.tools.tools import (
    JsonObject,
)

HISTORY_ACTIONS = ("overview", "search", "read", "around")


HISTORY_SUPPORTED_ROLES = (
    "system",
    "user",
    "assistant",
    "tool",
    "note",
    "error",
    "run_summary",
    "agent_takeover",
)


HISTORY_DEFAULT_ROLES = ("user", "assistant", "error")


HISTORY_MATCH_MODES = ("all_terms", "phrase", "any_term")


HISTORY_DIRECTIONS = ("start", "end")


HISTORY_CURSOR_VERSION = 2


_ACTION_FIELDS = {
    "overview": frozenset({"action", "limit", "cursor"}),
    "search": frozenset({"action", "query", "checkpoint", "roles", "match", "limit", "cursor"}),
    "read": frozenset({"action", "checkpoint", "roles", "direction", "limit", "cursor"}),
    "around": frozenset(
        {"action", "message_id", "checkpoint", "roles", "before", "after", "cursor"}
    ),
}


_CURSOR_KEYS = frozenset(
    {
        "v",
        "session_id",
        "generation_id",
        "action",
        "snapshot_seq",
        "snapshot_id",
        "snapshot_ordinal",
        "checkpoint",
        "checkpoint_seq",
        "checkpoint_id",
        "roles",
        "direction",
        "query",
        "match",
        "limit",
        "before",
        "after",
        "message_id",
        "next_seq",
        "within_offset",
    }
)


class _HistoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Snapshot:
    generation_id: str
    checkpoints: tuple[SessionHistoryCheckpoint, ...]

    @property
    def latest(self) -> SessionHistoryCheckpoint:
        return self.checkpoints[-1]


@dataclass(frozen=True)
class _Request:
    action: str
    checkpoint: int | None
    roles: tuple[str, ...]
    direction: str
    query: str | None
    match: str
    limit: int
    before: int
    after: int
    message_id: str | None
    next_sequence: int | None = None
    within_offset: int = 0


def _cursor_payload(arguments: JsonObject, session_id: str) -> JsonObject | None:
    cursor = arguments.get("cursor")
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor.strip():
        raise _HistoryError("invalid_arguments", "cursor must be a non-blank string")
    if set(arguments) != {"action", "cursor"}:
        raise _HistoryError(
            "invalid_arguments", "A cursor continuation accepts only action and cursor."
        )
    payload = _decode_cursor(cursor)
    if payload.get("session_id") != session_id:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("action") != arguments.get("action"):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    return payload


def _request_from_arguments(arguments: JsonObject, snapshot: _Snapshot) -> _Request:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in HISTORY_ACTIONS:
        raise _HistoryError("invalid_arguments", "action must be a supported History action")
    unsupported = sorted(set(arguments) - _ACTION_FIELDS[action])
    if unsupported:
        raise _HistoryError(
            "invalid_arguments",
            f"Unsupported arguments for {action}: {', '.join(unsupported)}",
        )
    checkpoint = _optional_checkpoint(arguments.get("checkpoint"), snapshot)
    roles = _roles(arguments.get("roles"))
    query: str | None = None
    match = "all_terms"
    direction = "start"
    limit = 10 if action in {"overview", "search"} else 20
    before = 2
    after = 2
    message_id: str | None = None

    if action == "search":
        raw_query = arguments.get("query")
        if not isinstance(raw_query, str) or not raw_query.strip():
            raise _HistoryError("invalid_arguments", "search requires a non-blank query")
        query = raw_query.strip()
        match = _enum(arguments.get("match", match), HISTORY_MATCH_MODES, "match")
    if action == "read":
        direction = _enum(arguments.get("direction", direction), HISTORY_DIRECTIONS, "direction")
    if action in {"overview", "search", "read"}:
        limit = _bounded_int(arguments.get("limit", limit), "limit", 1, 100)
    if action == "around":
        raw_message_id = arguments.get("message_id")
        if not isinstance(raw_message_id, str) or not raw_message_id.strip():
            raise _HistoryError("invalid_arguments", "around requires a non-blank message_id")
        message_id = raw_message_id.strip()
        before = _bounded_int(arguments.get("before", before), "before", 0, 100)
        after = _bounded_int(arguments.get("after", after), "after", 0, 100)

    return _Request(
        action=action,
        checkpoint=checkpoint,
        roles=roles,
        direction=direction,
        query=query,
        match=match,
        limit=limit,
        before=before,
        after=after,
        message_id=message_id,
    )


def _validate_history_action_arguments(arguments: JsonObject, action: str) -> None:
    unsupported = sorted(set(arguments) - _ACTION_FIELDS[action])
    if unsupported:
        raise _HistoryError(
            "invalid_arguments",
            f"Unsupported arguments for {action}: {', '.join(unsupported)}",
        )
    if "cursor" in arguments:
        cursor = arguments.get("cursor")
        if not isinstance(cursor, str) or not cursor.strip():
            raise _HistoryError("invalid_arguments", "cursor must be a non-blank string")
        if set(arguments) != {"action", "cursor"}:
            raise _HistoryError(
                "invalid_arguments",
                "A cursor continuation accepts only action and cursor.",
            )
        return
    if action == "search":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise _HistoryError("invalid_arguments", "search requires a non-blank query")
    if action == "around":
        message_id = arguments.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            raise _HistoryError("invalid_arguments", "around requires a non-blank message_id")


def _request_from_cursor(
    payload: JsonObject,
    snapshot: _Snapshot,
    session_id: str,
) -> _Request:
    if set(payload) != _CURSOR_KEYS or payload.get("v") != HISTORY_CURSOR_VERSION:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("session_id") != session_id:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("generation_id") != snapshot.generation_id:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("snapshot_seq") != snapshot.latest.sequence:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("snapshot_id") != snapshot.latest.message_id:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("snapshot_ordinal") != snapshot.latest.ordinal:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    action = payload.get("action")
    if not isinstance(action, str) or action not in HISTORY_ACTIONS:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    roles = _cursor_roles(payload.get("roles"))
    checkpoint = payload.get("checkpoint")
    if checkpoint is not None and (
        isinstance(checkpoint, bool) or not isinstance(checkpoint, int) or checkpoint < 1
    ):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    selected = _checkpoint(snapshot, checkpoint, cursor=True)
    if payload.get("checkpoint_seq") != (selected.sequence if selected is not None else None):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("checkpoint_id") != (selected.message_id if selected is not None else None):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")

    direction = payload.get("direction")
    match = payload.get("match")
    query = payload.get("query")
    message_id = payload.get("message_id")
    if direction not in HISTORY_DIRECTIONS or match not in HISTORY_MATCH_MODES:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if query is not None and not isinstance(query, str):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if message_id is not None and not isinstance(message_id, str):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    limit = _cursor_int(payload.get("limit"), minimum=1, maximum=100)
    before = _cursor_int(payload.get("before"), minimum=0, maximum=100)
    after = _cursor_int(payload.get("after"), minimum=0, maximum=100)
    next_sequence = payload.get("next_seq")
    if next_sequence is not None:
        next_sequence = _cursor_int(next_sequence, minimum=0)
    within_offset = _cursor_int(payload.get("within_offset"), minimum=0)
    if next_sequence is None:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if action == "overview" and (
        checkpoint is not None
        or roles != HISTORY_DEFAULT_ROLES
        or direction != "start"
        or query is not None
        or match != "all_terms"
        or before != 2
        or after != 2
        or message_id is not None
    ):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if action == "search" and (
        not isinstance(query, str)
        or not query.strip()
        or direction != "start"
        or before != 2
        or after != 2
        or message_id is not None
    ):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if action == "read" and (
        query is not None
        or match != "all_terms"
        or before != 2
        or after != 2
        or message_id is not None
    ):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if action == "around" and (
        query is not None
        or match != "all_terms"
        or direction != "start"
        or limit != 20
        or not isinstance(message_id, str)
        or not message_id.strip()
    ):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    return _Request(
        action=action,
        checkpoint=checkpoint,
        roles=roles,
        direction=direction,
        query=query,
        match=match,
        limit=limit,
        before=before,
        after=after,
        message_id=message_id,
        next_sequence=next_sequence,
        within_offset=within_offset,
    )


def _checkpoint(
    snapshot: _Snapshot,
    ordinal: int | None,
    *,
    cursor: bool = False,
) -> SessionHistoryCheckpoint | None:
    if ordinal is None:
        return None
    selected = next(
        (checkpoint for checkpoint in snapshot.checkpoints if checkpoint.ordinal == ordinal),
        None,
    )
    if selected is not None:
        return selected
    if cursor:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    raise _HistoryError(
        "checkpoint_not_found",
        f"Checkpoint {ordinal} was not found; available checkpoints: 1-{snapshot.latest.ordinal}.",
    )


def _optional_checkpoint(value: Any, snapshot: _Snapshot) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _HistoryError("invalid_arguments", "checkpoint must be a positive integer")
    _checkpoint(snapshot, value)
    return value


def _roles(value: Any) -> tuple[str, ...]:
    if value is None:
        return HISTORY_DEFAULT_ROLES
    if not isinstance(value, list) or not all(isinstance(role, str) for role in value):
        raise _HistoryError("invalid_arguments", "roles must be an array of role names")
    if len(set(value)) != len(value) or any(role not in HISTORY_SUPPORTED_ROLES for role in value):
        raise _HistoryError("invalid_arguments", "roles contains an unsupported or duplicate role")
    return tuple(value)


def _cursor_roles(value: Any) -> tuple[str, ...]:
    try:
        return _roles(value)
    except _HistoryError as error:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.") from error


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _HistoryError("invalid_arguments", f"{field} must be between {minimum} and {maximum}")
    return value


def _cursor_int(value: Any, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if maximum is not None and value > maximum:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    return value


def _enum(value: Any, values: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in values:
        raise _HistoryError("invalid_arguments", f"{field} is invalid")
    return value


def _encode_cursor(payload: JsonObject) -> str:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(body).digest()
    return base64.urlsafe_b64encode(body + digest).decode("ascii").rstrip("=")


def _decode_cursor(token: str) -> JsonObject:
    try:
        padded = token + "=" * (-len(token) % 4)
        encoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(encoded) <= hashlib.sha256().digest_size:
            raise ValueError
        body = encoded[: -hashlib.sha256().digest_size]
        digest = encoded[-hashlib.sha256().digest_size :]
        if not hmac.compare_digest(hashlib.sha256(body).digest(), digest):
            raise ValueError
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (binascii.Error, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.") from error
