"""Session metadata validation and history cursor encoding."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from core.chat.errors import ChatSessionError
from core.sessions._types import (
    _CHAT_HISTORY_CURSOR_PREFIX,
    FORK_SOURCE_META_KEY,
    SESSION_ID_PATTERN,
    SESSION_RUN_KINDS_META_KEY,
    SESSION_TERMINAL_RUN_STATUSES,
    SESSION_TITLE_MAX_LENGTH,
    JsonObject,
    SessionAddress,
)
from core.sessions.errors import SessionPageCursorError
from core.settings import is_valid_agent_id


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


def _format_timestamp(timestamp: datetime | None) -> str:
    value = datetime.now(UTC) if timestamp is None else timestamp.astimezone(UTC)
    return value.isoformat().replace("Z", "+00:00")


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


def _decode_state_json_value(payload: str, name: str, expected_type: type[Any]) -> Any:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ChatSessionError(f"invalid {name}") from exc
    if not isinstance(value, expected_type):
        raise ChatSessionError(f"invalid {name}")
    return value


def _session_list_summary_from_state(state: Any) -> JsonObject:
    summary: JsonObject = {
        "id": str(state["session_id"]),
        "project_id": str(state["project_id"]) or None,
        "agent_id": str(state["agent_id"]),
        "created_at": str(state["created_at"]),
        "last_active_at": str(state["last_active_at"]),
    }
    for key in (
        "title",
        "auto_title",
        "source_channel_id",
        "platform",
        "platform_conv_id",
    ):
        value = state[key]
        if value is not None:
            summary[key] = str(value)
    if state["is_subagent_session"] is not None:
        summary["is_subagent_session"] = bool(state["is_subagent_session"])
    for key, column, expected_type in (
        ("subagent_parent", "subagent_parent_json", dict),
        (FORK_SOURCE_META_KEY, "fork_source_json", dict),
        (SESSION_RUN_KINDS_META_KEY, "run_kinds_json", list),
        ("compaction_policy", "compaction_policy_json", dict),
    ):
        payload = state[column]
        if payload is not None:
            summary[key] = _decode_state_json_value(
                payload,
                f"Session {key}",
                expected_type,
            )
    summary.update(_completion_activity_from_state(state))
    return summary


def _valid_latest_completion(activity: JsonObject) -> JsonObject | None:
    latest = activity.get("latest_completion")
    if latest is None:
        return None
    if not isinstance(latest, dict):
        raise ChatSessionError("session activity latest_completion must be an object")
    run_id, status, timestamp = latest.get("run_id"), latest.get("status"), latest.get("timestamp")
    if (
        not isinstance(run_id, str)
        or not run_id
        or status not in SESSION_TERMINAL_RUN_STATUSES
        or not isinstance(timestamp, str)
        or not timestamp
    ):
        raise ChatSessionError("session activity latest_completion is invalid")
    return {"run_id": run_id, "status": status, "timestamp": timestamp}


def _completion_activity_payload(activity: JsonObject) -> JsonObject:
    latest = _valid_latest_completion(activity)
    latest_id = latest["run_id"] if latest else None
    if latest is None or activity.get("read_run_id") == latest_id:
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
        "unread_run_id": latest["run_id"],
        "unread_run_status": latest["status"],
        "unread_run_at": latest["timestamp"],
    }


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
