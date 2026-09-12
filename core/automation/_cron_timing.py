"""Cron wall-clock waits and bounded retry timing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

# Recheck wall time after bounded monotonic sleeps so NTP corrections do not
# postpone jobs. An application timezone update may wake the current sleeper.
_ONCE_RETRY_DELAY_SECONDS = 60.0

_ONCE_RETRY_BACKOFF_FACTOR = 2.0

_ONCE_RETRY_MAX_DELAY_SECONDS = 3600.0

_WALL_CLOCK_RECHECK_SECONDS = 60.0


def _once_retry_delay(attempt: int) -> float:
    """Backoff delay in seconds for the Nth (1-based) failed once-job fire."""
    exponent = max(attempt - 1, 0)
    delay = _ONCE_RETRY_DELAY_SECONDS * (_ONCE_RETRY_BACKOFF_FACTOR**exponent)
    return min(delay, _ONCE_RETRY_MAX_DELAY_SECONDS)


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _sleep_until_utc(
    target_utc: datetime, *, wake_event: asyncio.Event | None = None
) -> bool:
    """Sleep until a UTC instant, returning false when a schedule change wakes it."""
    while True:
        remaining_seconds = (target_utc - _utc_now()).total_seconds()
        if remaining_seconds <= 0:
            return True
        nap_seconds = min(remaining_seconds, _WALL_CLOCK_RECHECK_SECONDS)
        if wake_event is None:
            await asyncio.sleep(nap_seconds)
            continue
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=nap_seconds)
        except TimeoutError:
            continue
        return False


def _utc_now_iso() -> str:
    return _utc_now().isoformat()
