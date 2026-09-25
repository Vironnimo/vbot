"""Local records of Live calls: every Tool call and delegation, as JSON lines.

The records measure how voice and backend models use the Live Tools. They stay
on this machine in ``<data>/artifacts/live-calls/<YYYY-MM-DD>.jsonl`` (one file
per UTC day), hold what the Models sent and read (requests, Tool arguments,
result texts), and never enter the logs. Files older than the retention window
are deleted when the first record of a new day is written.

One ordered thread appends, so the Event Loop never waits for the disk and the
records of a call keep their order. Recording never fails a call: a full
backlog drops the record, and write failures are logged without content.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from core.utils.logging import get_logger
from core.utils.workers import OrderedWorker

JsonObject = dict[str, Any]

_LOGGER = get_logger("server.live")

RETENTION_DAYS = 30
# Records waiting for the writer thread; beyond this backlog new ones are dropped.
MAX_PENDING_RECORDS = 256
_SUFFIX = ".jsonl"

_WRITER = OrderedWorker(name="live-records")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LiveCallRecorder:
    """Append Live call records to the day's file and prune old days."""

    def __init__(
        self,
        directory: Path,
        *,
        retention_days: int = RETENTION_DAYS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._directory = directory
        self._retention = timedelta(days=retention_days)
        self._clock = clock
        # Written and read on the writer thread only.
        self._pruned_on: date | None = None

    def record(self, call_id: str, event: JsonObject) -> None:
        """Hand one record to the writer thread without waiting."""
        now = self._clock()
        line = json.dumps(
            {"at": now.isoformat(), "call_id": call_id, **event},
            ensure_ascii=False,
            default=str,
        )
        if not _WRITER.hand_off(
            partial(self._append, now.date(), line + "\n"), limit=MAX_PENDING_RECORDS
        ):
            _LOGGER.warning("Live call record dropped: the writer is behind")

    async def drain(self) -> None:
        """Wait until every record handed off so far is written."""
        await _WRITER.drain()

    def _append(self, day: date, text: str) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            if self._pruned_on != day:
                self._prune(day)
                self._pruned_on = day
            with (self._directory / f"{day.isoformat()}{_SUFFIX}").open(
                "a", encoding="utf-8", newline="\n"
            ) as handle:
                handle.write(text)
        except OSError as exc:
            _LOGGER.warning("Live call record failed: error_type=%s", type(exc).__name__)

    def _prune(self, today: date) -> None:
        oldest = today - self._retention
        for path in self._directory.glob(f"*{_SUFFIX}"):
            try:
                day = date.fromisoformat(path.name.removesuffix(_SUFFIX))
            except ValueError:
                continue
            if day < oldest:
                try:
                    path.unlink()
                except OSError as exc:
                    _LOGGER.warning(
                        "Old Live call record not deleted: error_type=%s", type(exc).__name__
                    )


__all__ = ["LiveCallRecorder"]
