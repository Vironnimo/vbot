"""The retention period: how long an archive entry rests before it is deleted permanently.

An entry's ``purge_at`` is computed on read from its ``retention_start`` and the
current period (``archive.retention_days``), so a changed period applies to
existing entries at once. Retention never deletes ``files`` entries, entries
whose ``user_folders`` fact names folders the user may own rather than copies
vBot made, or ``recovered`` entries (a payload found without its entry, whose
age nothing records); only a purge that names them deletes those.

The retention sweep deletes due entries in the background, on the ``archive``
worker and never on the Event Loop. The first sweep runs a minute after start,
later ones every hour, and one runs at once when the period changes or a manual
purge left an entry pending. A sweep first continues entries a stopped or failed
purge left ``purging``, then purges due entries oldest first, one entry per
worker call so manual purges never wait behind a whole sweep. A stop request
ends the sweep before the next Session or file it would delete; the entry stays
``purging`` and the next sweep continues it.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from typing import TYPE_CHECKING

from core.archive import _purge
from core.sessions import (
    ARCHIVE_KIND_FILES,
    ARCHIVE_ORIGIN_RECOVERED,
    ARCHIVE_STATE_ARCHIVED,
    ARCHIVE_STATE_PURGING,
    ArchiveEntry,
)
from core.settings.normalizers import ARCHIVE_SETTING_DEFAULTS
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp, parse_canonical_timestamp

if TYPE_CHECKING:
    from core.archive.archive import ArchiveServices

_LOGGER = get_logger("archive")

# The first sweep waits until startup has settled; later sweeps run hourly.
FIRST_SWEEP_DELAY_SECONDS = 60.0
SWEEP_INTERVAL_SECONDS = 3600.0

# Due entries are read in pages of this size.
_DUE_PAGE = 100

# Purge reasons in the owner log line.
REASON_RETENTION = "retention"
REASON_RESUMED = "resumed"
ACTOR = "retention"

# One ERROR per data directory while its sweeps keep failing the same way, one
# INFO when they work again.
_SWEEP_FAILURES = LoggedConditions()


def default_retention_days() -> int | None:
    """The default retention period in days; ``None`` keeps entries until they are deleted."""
    days: int | None = ARCHIVE_SETTING_DEFAULTS["retention_days"]
    return days


def may_hold_user_folders(entry: ArchiveEntry) -> bool:
    """Whether ``entry`` may hold folders the user owns rather than copies vBot made.

    True for ``files`` entries (older archived content) and for entries whose
    ``user_folders`` fact names such folders. Neither retention nor a purge of
    every matching entry deletes one; only a purge that names it does.
    """
    return entry.kind == ARCHIVE_KIND_FILES or bool(entry.facts.get("user_folders"))


def purge_at(entry: ArchiveEntry, retention_days: int | None) -> str | None:
    """When the retention period of ``entry`` ends, or ``None`` when retention never deletes it.

    Only a resting (``archived``) entry is due; a ``purging`` one is already
    being deleted, and the other states belong to an operation in progress. A
    ``recovered`` entry is never due: its payload was found without its entry,
    so nothing records how old it is.
    """
    if retention_days is None or entry.state != ARCHIVE_STATE_ARCHIVED:
        return None
    if may_hold_user_folders(entry) or entry.origin == ARCHIVE_ORIGIN_RECOVERED:
        return None
    try:
        start = parse_canonical_timestamp(entry.retention_start)
    except ValueError:
        return None
    return format_canonical_timestamp(start + timedelta(days=retention_days))


async def _wait(wake: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds`` or until ``wake`` is set (the sweep timer's test seam)."""
    with suppress(TimeoutError):
        await asyncio.wait_for(wake.wait(), seconds)


class RetentionSweeper:
    """The background task that purges due and interrupted archive entries.

    ``stopping`` is shared with the archive service, whose manual purges end
    at the same stop request; ``notify`` reports changed entries.
    """

    def __init__(
        self,
        services: ArchiveServices,
        stopping: threading.Event,
        notify: Callable[[], None],
    ) -> None:
        self._services = services
        self._stopping = stopping
        self._notify = notify
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Start sweeping; call inside the serving Event Loop."""
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = self._loop.create_task(self._run(self._wake), name="archive-retention")

    def stop(self) -> None:
        """Stop sweeping; a purge in progress ends before its next Session or tree."""
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()

    async def aclose(self) -> None:
        """Stop sweeping and wait until the purge in progress has ended."""
        self.stop()
        task = self._task
        if task is not None and not task.done():
            # A cancelled worker call returns only after the started purge settles.
            await asyncio.gather(task, return_exceptions=True)

    def wake(self) -> None:
        """Sweep at once; does nothing while the sweep is not running. Thread-safe."""
        task, loop, wake = self._task, self._loop, self._wake
        if task is None or task.done() or loop is None or wake is None:
            return
        if self._stopping.is_set():
            return
        with suppress(RuntimeError):  # The Event Loop has closed.
            loop.call_soon_threadsafe(wake.set)

    async def _run(self, wake: asyncio.Event) -> None:
        delay = FIRST_SWEEP_DELAY_SECONDS
        try:
            while not self._stopping.is_set():
                await _wait(wake, delay)
                wake.clear()
                try:
                    await self._sweep()
                except Exception as error:
                    # Unexpected: entry failures are handled inside the purge.
                    if _SWEEP_FAILURES.started(self._services.data_dir, type(error).__name__):
                        _LOGGER.error(
                            "Archive retention sweep failed; the next sweep retries it: %s",
                            error,
                            exc_info=True,
                        )
                else:
                    if _SWEEP_FAILURES.ended(self._services.data_dir):
                        _LOGGER.info("Archive retention sweep no longer fails")
                delay = SWEEP_INTERVAL_SECONDS
        except asyncio.CancelledError:
            return

    async def _sweep(self) -> None:
        services = self._services
        _LOGGER.debug("Archive retention sweep started")
        purging = await _purge.PURGE_WORKERS.run(_purging_ids, services)
        for entry_id in purging:
            if self._stopping.is_set():
                return
            await self._purge(entry_id)
        after: ArchiveEntry | None = None
        while not self._stopping.is_set():
            due, after = await _purge.PURGE_WORKERS.run(_due_page, services, after)
            for entry_id in due:
                if self._stopping.is_set():
                    return
                await self._purge(entry_id)
            if after is None:
                break
        _LOGGER.debug("Archive retention sweep passed")

    async def _purge(self, entry_id: str) -> None:
        changed = await _purge.PURGE_WORKERS.run(
            _purge_if_due, self._services, entry_id, self._stopping
        )
        if changed:
            self._notify()


def _purging_ids(services: ArchiveServices) -> tuple[str, ...]:
    return tuple(
        entry.entry_id
        for entry in services.sessions.archive_ledger.unsettled()
        if entry.state == ARCHIVE_STATE_PURGING
    )


def _due_page(
    services: ArchiveServices, after: ArchiveEntry | None
) -> tuple[tuple[str, ...], ArchiveEntry | None]:
    """One page of due entry ids, and where the next page starts (``None`` at the end)."""
    days = services.retention_days()
    if days is None:
        return (), None
    now = services.clock()
    before = format_canonical_timestamp(now - timedelta(days=days))
    entries = services.sessions.archive_ledger.due(before, after=after, limit=_DUE_PAGE)
    deadline = format_canonical_timestamp(now)
    due = tuple(
        entry.entry_id
        for entry in entries
        if (ends := purge_at(entry, days)) is not None and ends <= deadline
    )
    return due, entries[-1] if len(entries) == _DUE_PAGE else None


def _purge_if_due(services: ArchiveServices, entry_id: str, stopping: threading.Event) -> bool:
    """Purge ``entry_id`` when it is still ``purging`` or due now; whether it changed."""
    entry = services.sessions.archive_ledger.entry(entry_id)
    if entry is None:
        return False
    if entry.state == ARCHIVE_STATE_PURGING:
        reason = REASON_RESUMED
    else:
        ends = purge_at(entry, services.retention_days())
        if ends is None or ends > format_canonical_timestamp(services.clock()):
            return False
        reason = REASON_RETENTION
    _purge.purge_entries(services, [entry], reason=reason, actor=ACTOR, stopping=stopping)
    return True


__all__ = [
    "FIRST_SWEEP_DELAY_SECONDS",
    "SWEEP_INTERVAL_SECONDS",
    "RetentionSweeper",
    "default_retention_days",
    "may_hold_user_folders",
    "purge_at",
]
