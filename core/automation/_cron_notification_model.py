"""Persisted outbound messages, independent of a job's later delivery edits."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

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
        "retry_at",
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
    retry_at: float = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

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
        retry_at = value.get("retry_at", 0)
        if (
            isinstance(retry_at, bool)
            or not isinstance(retry_at, (int, float))
            or not 0 <= retry_at < 1e12
        ):
            raise ValueError("notification retry_at must be a finite non-negative timestamp")
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
            retry_at=retry_at,
        )
