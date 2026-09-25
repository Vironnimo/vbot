"""Session metadata validation and history cursor encoding."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from typing import Any

from core.chat.errors import ChatSessionError
from core.runs import RunKind
from core.sessions._types import (
    _CHAT_HISTORY_CURSOR_PREFIX,
    SESSION_ID_PATTERN,
    SESSION_TITLE_MAX_LENGTH,
    JsonObject,
    SessionAddress,
)
from core.sessions.errors import SessionPageCursorError
from core.settings import is_valid_agent_id

_RUN_KIND_VALUES = frozenset(kind.value for kind in RunKind)


def _validate_agent_id(agent_id: str) -> None:
    if not is_valid_agent_id(agent_id):
        raise ChatSessionError(
            "agent id must be 1-64 characters using only letters, numbers, hyphen, or underscore"
        )


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ChatSessionError(
            "session id must be 1-128 characters of ASCII letters, digits, hyphen, "
            "or underscore and must not start with punctuation"
        )


def _normalize_session_title(title: str) -> str | None:
    if not isinstance(title, str):
        raise ChatSessionError("session title must be a string")
    value = " ".join(title.split())
    return value[:SESSION_TITLE_MAX_LENGTH] or None


def _new_prompt_cache_affinity_id() -> str:
    return uuid.uuid4().hex


def _default_prompt_cache_affinity_id(address: SessionAddress) -> str:
    encoded = json.dumps(
        [address.project_id, address.agent_id, address.session_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def _is_prompt_cache_affinity_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(char in "0123456789abcdef" for char in value)
    )


def _decode_state_object(payload: str, name: str) -> JsonObject:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ChatSessionError(f"invalid {name}") from exc
    if not isinstance(value, dict):
        raise ChatSessionError(f"invalid {name}")
    return value


def _completion_activity_from_state(state: Any) -> JsonObject:
    latest_id = state["latest_completion_run_id"]
    if latest_id is None or state["read_completion_run_id"] == latest_id:
        return {
            "latest_completion_run_id": latest_id,
            "has_unread_completion": False,
            "unread_run_id": None,
            "unread_run_status": None,
            "unread_run_at": None,
        }
    return {
        "latest_completion_run_id": latest_id,
        "has_unread_completion": True,
        "unread_run_id": latest_id,
        "unread_run_status": state["latest_completion_status"],
        "unread_run_at": state["latest_completion_at"],
    }


def _decode_chat_history_cursor(value: str) -> tuple[str, int] | None:
    if not value.startswith(_CHAT_HISTORY_CURSOR_PREFIX):
        return None
    try:
        token = value.removeprefix(_CHAT_HISTORY_CURSOR_PREFIX)
        encoded = base64.urlsafe_b64decode((token + "=" * (-len(token) % 4)).encode("ascii"))
        digest_size = hashlib.sha256().digest_size
        if len(encoded) <= digest_size:
            raise ValueError
        body = encoded[:-digest_size]
        if not hmac.compare_digest(hashlib.sha256(body).digest(), encoded[-digest_size:]):
            raise ValueError
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"generation_id", "sequence"}:
            raise ValueError
        generation_id = payload["generation_id"]
        sequence = payload["sequence"]
        if (
            not isinstance(generation_id, str)
            or not generation_id
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
        ):
            raise ValueError
        return generation_id, sequence
    except (binascii.Error, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise SessionPageCursorError("before cursor is invalid") from error
