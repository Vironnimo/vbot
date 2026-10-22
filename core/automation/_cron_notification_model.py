"""Persisted outbound messages, independent of a job's later delivery edits."""

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
    )
)


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

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
        retry_after_at = value.get("retry_after_at")
        if retry_after_at is not None and not is_canonical_timestamp(retry_after_at):
            raise ValueError("notification retry_after_at must be a UTC timestamp or null")
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
            retry_after_at=retry_after_at,
        )
