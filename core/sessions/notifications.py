"""Shared notification policy, independent of an outcome's read state."""

import sqlite3
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING

from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.sessions._types import JsonObject, SessionAddress
    from core.sessions.store import SessionStore


class NotificationPolicy(StrEnum):
    ALL = "all"
    ERRORS = "errors"
    MUTED = "muted"


def notification_allowed(policy: str, status: str) -> bool:
    return policy == NotificationPolicy.ALL or (
        policy == NotificationPolicy.ERRORS and status in {"failed", "interrupted"}
    )


class SessionNotifications:
    """Notification settings and read acknowledgements on canonical Session data."""

    def __init__(self, store: SessionStore, read: Callable[[SessionAddress, str], None]) -> None:
        self._store = store
        self._read = read

    def set_policy(self, address: SessionAddress, policy: str) -> None:
        from core.sessions import _store_notifications

        changed = self._store._execute_write(
            lambda db: _store_notifications.set_policy(db, address, policy)
        )
        if changed:
            get_logger("sessions").info(
                "Session notification policy changed (agent=%s project=%s session=%s policy=%s)",
                address.agent_id,
                address.project_id,
                address.session_id,
                policy,
            )

    def mark_read(self, address: SessionAddress, run_ids: Sequence[str]) -> JsonObject:
        from core.sessions import _store_notifications, _store_runs, _store_values

        def write(db: sqlite3.Connection) -> tuple[list[str], JsonObject]:
            changed = _store_notifications.mark_runs_read(db, address, run_ids)
            activity = _store_runs._completion_activity(
                db, int(_store_values._require_live(db, address)["session_key"])
            )
            from core.sessions._metadata import _completion_activity_from_state

            return changed, _completion_activity_from_state(activity)

        changed, activity = self._store._execute_write(write)
        for run_id in changed:
            self._read(address, run_id)
        return activity | {"marked_read": bool(changed), "read_run_ids": list(run_ids)}

    def schedule_activity(self, busy: Sequence[SessionAddress] = ()) -> dict[str, JsonObject]:
        from core.sessions import _store_notifications

        return self._store._read(lambda db: _store_notifications.schedule_activity(db, busy))

    def schedule_runs(
        self, schedule_id: str, *, before: int | None = None, limit: int = 5
    ) -> JsonObject:
        from core.sessions import _store_notifications

        if not 1 <= limit <= 50 or (before is not None and before < 1):
            raise ValueError("Invalid schedule history page")
        return self._store._read(
            lambda db: _store_notifications.schedule_runs(
                db, schedule_id, before=before, limit=limit
            )
        )

    def mark_schedule_read(self, schedule_id: str, through: int) -> int:
        from core.sessions import _store_notifications

        changed = self._store._execute_write(
            lambda db: _store_notifications.mark_schedule_read(db, schedule_id, through)
        )
        for address, run_id in changed:
            self._read(address, run_id)
        return len(changed)

    def adopt_schedule_runs(
        self, sources: Sequence[tuple[str, str, str | None, str | None, str]]
    ) -> None:
        from core.sessions import _store_notifications

        if sources:
            self._store._execute_write(
                lambda db: _store_notifications.adopt_schedule_runs(db, sources)
            )

    def run_status(self, address: SessionAddress, run_id: str) -> JsonObject:
        from core.sessions import _store_notifications

        return self._store._read(lambda db: _store_notifications.run_status(db, address, run_id))
