"""Persisted outbound messages and unread alerts, independent of a job's later delivery edits."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal, cast

from core.utils.timestamps import is_canonical_timestamp, parse_canonical_timestamp

NOTIFICATION_FIELDS = frozenset(
    (
        "id",
        "agent_id",
        "channel_id",
        "platform_target",
        "thread_id",
        "text",
        "note",
        "session_id",
        "state",
        "error",
        "retry_after_at",
        "channel_missing_since",
    )
)

ALERT_FIELDS = frozenset(("message", "at", "kind"))

# The longest alert text a job keeps; alert texts embed an error of at most 500 characters.
ALERT_MAX_CHARS = 2000

type AlertKind = Literal["failure", "delivery"]


@dataclass(frozen=True, slots=True)
class CronNotification:
    id: str
    agent_id: str
    channel_id: str
    platform_target: str
    text: str
    note: str
    thread_id: str | None = None
    session_id: str | None = None
    state: Literal["pending", "sending", "uncertain"] = "pending"
    error: str | None = None
    # Canonical UTC timestamp before which a known-unsent message waits; None: send now.
    retry_after_at: str | None = None
    # Canonical UTC timestamp since which the message's Channel no longer exists.
    channel_missing_since: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.channel_missing_since is None:
            del data["channel_missing_since"]
        return data

    def due(self, now: datetime) -> bool:
        return self.retry_after_at is None or parse_canonical_timestamp(self.retry_after_at) <= now

    @classmethod
    def from_dict(cls, value: Any) -> CronNotification:
        if not isinstance(value, dict):
            raise ValueError("notification must be an object")
        for key in ("id", "agent_id", "channel_id", "platform_target", "text", "note"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise ValueError(f"notification {key} must be a non-empty string")
        for key in ("thread_id", "session_id", "error"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError(f"notification {key} must be a string or null")
        state = value.get("state", "pending")
        if state not in ("pending", "sending", "uncertain"):
            raise ValueError("notification state must be pending, sending or uncertain")
        for key in ("retry_after_at", "channel_missing_since"):
            stamp = value.get(key)
            if stamp is not None and not is_canonical_timestamp(stamp):
                raise ValueError(f"notification {key} must be a UTC timestamp or null")
        return cls(
            id=value["id"],
            agent_id=value["agent_id"],
            channel_id=value["channel_id"],
            platform_target=value["platform_target"],
            text=value["text"],
            note=value["note"],
            thread_id=value.get("thread_id"),
            session_id=value.get("session_id"),
            state=cast('Literal["pending", "sending", "uncertain"]', state),
            error=value.get("error"),
            retry_after_at=value.get("retry_after_at"),
            channel_missing_since=value.get("channel_missing_since"),
        )


@dataclass(frozen=True, slots=True)
class CronAlert:
    """An alert about a job that the user has not acknowledged yet.

    ``failure``: a failure no Run reports (a failed check, a Run that could not
    start); the job's next success ends it. ``delivery``: a chat message whose
    delivery is uncertain or that was dropped because its Channel is gone;
    retrying the delivery ends it. Acknowledging ends either.
    """

    message: str
    at: str
    kind: AlertKind = "failure"

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "at": self.at, "kind": self.kind}

    @classmethod
    def from_dict(cls, value: Any) -> CronAlert:
        if not isinstance(value, dict):
            raise ValueError("unread_alert must be an object or null")
        message, at, kind = value.get("message"), value.get("at"), value.get("kind", "failure")
        if not isinstance(message, str) or not message.strip() or len(message) > ALERT_MAX_CHARS:
            raise ValueError(
                f"unread_alert.message must be a non-empty string of at most "
                f"{ALERT_MAX_CHARS} characters"
            )
        if not is_canonical_timestamp(at):
            raise ValueError("unread_alert.at must be a UTC timestamp")
        if kind not in ("failure", "delivery"):
            raise ValueError("unread_alert.kind must be failure or delivery")
        return cls(message=message, at=cast(str, at), kind=cast(AlertKind, kind))
