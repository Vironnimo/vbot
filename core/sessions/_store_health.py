"""Cheap Session index status and the last explicit coverage check of this store."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable

from core.database import DatabaseHealth
from core.sessions import _store_fts
from core.sessions.errors import FtsHealth
from core.utils.timestamps import utc_now_timestamp


class SessionHealth:
    """Keep coverage evidence local to the Session store, without writing on reads.

    Status reads lifecycle markers only. An explicit check retains its result
    for this store's lifetime; a result from an older index generation is
    historical evidence, never the current generation's health.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_check: tuple[float, str, FtsHealth] | None = None

    def check(self, read: Callable[[], FtsHealth]) -> FtsHealth:
        started = time.monotonic()
        try:
            health = read()
        except Exception as exc:
            health = FtsHealth(state="unavailable", reason=f"FTS health check failed: {exc}")
        checked_at = utc_now_timestamp()
        with self._lock:
            # A check of an older read snapshot can finish after a newer one.
            if self._last_check is None or started >= self._last_check[0]:
                self._last_check = (started, checked_at, health)
        return health

    def status(
        self, connection: sqlite3.Connection, *, verify_search: bool = False
    ) -> DatabaseHealth:
        if verify_search:
            self.check(
                lambda: _store_fts._fts_health_from_connection(connection, verify_coverage=True)
            )
        current = _store_fts._fts_health_from_connection(connection, verify_coverage=False)
        with self._lock:
            last_check = self._last_check
        reported = current
        evidence = None
        if last_check is not None:
            _started, checked_at, checked = last_check
            evidence = {
                "checked_at": checked_at,
                "state": checked.state,
                "reason": checked.reason,
                "generation": checked.generation,
            }
            # Lifecycle markers cannot refute a coverage failure. Keep that
            # failure visible until a successful check or a rebuilt generation.
            if (
                current.available
                and not checked.available
                and (checked.generation is None or checked.generation == current.generation)
            ):
                reported = checked
        return DatabaseHealth(
            "healthy" if reported.available else "degraded",
            None if reported.available else f"Session search: {reported.reason}",
            {
                "fts": {
                    "state": reported.state,
                    "reason": reported.reason,
                    "generation": current.generation,
                    "target_high_water": current.target_high_water,
                    "completed_high_water": current.completed_high_water,
                    "last_coverage_check": evidence,
                }
            },
        )
