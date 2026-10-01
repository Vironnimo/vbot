"""Permanent deletion ("purge") of archive entries.

Every purge runs on the one ``archive`` worker, so two purges never overlap in
this process; the entry's compare-and-set state keeps restores out. A purge
first claims its entries (``purging``), then brings the usage ledger up to date
(recorded usage outlives the Sessions), then deletes each entry's member
Sessions newest first, one transaction each, then its payload trees, then the
entry. It holds no compound mutation: an entry left ``purging`` by a failure, a
stop request or a crash keeps what is not deleted yet, and a later purge of it,
manual or by the retention sweep, continues.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.archive._operations import prune_empty_parents, stored_path
from core.archive._types import PendingPurge, PurgedEntry, PurgeOutcome, SkippedPurge
from core.sessions import (
    ARCHIVE_ROOT,
    ARCHIVE_STATE_PURGING,
    ArchiveEntry,
    ArchiveEntryBusyError,
    ArchiveEntryNotFoundError,
)
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.tree_move import remove_tree
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.archive.archive import ArchiveServices

_LOGGER = get_logger("archive")

PURGE_WORKERS = BoundedWorkerPool(name="archive", max_workers=1)

# The reason a pending entry reports when a stop request ended its purge.
STOPPED = "stopped"
USAGE_IMPORT_FAILED = "usage_import_failed"
# The reason a skipped entry reports when another operation held it.
BUSY = "busy"

# One WARNING per entry and failure kind until that entry is purged, and one
# per data directory while the usage import keeps failing.
_FAILURES = LoggedConditions()


class _StoppedError(Exception):
    """A stop request ended a purge between two steps."""


def purge_entries(
    services: ArchiveServices,
    entries: Sequence[ArchiveEntry],
    *,
    reason: str,
    actor: str,
    stopping: threading.Event | None = None,
) -> PurgeOutcome:
    """Purge ``entries`` one after another; one that does not finish is reported pending.

    Every entry is claimed first, so an entry reported pending stays
    ``purging`` and is continued later. An entry another operation holds is
    skipped unchanged, and one that is no longer in the archive is reported
    gone. When ``stopping`` is set, the purge ends before the next Session or tree.
    """
    ledger = services.sessions.archive_ledger
    claimed: list[ArchiveEntry] = []
    pending: list[PendingPurge] = []
    skipped: list[SkippedPurge] = []
    gone: list[str] = []
    for entry in entries:
        try:
            claimed.append(ledger.begin_purge(entry.entry_id))
        except ArchiveEntryNotFoundError:
            # Another operation deleted or restored it first: an expected outcome.
            _LOGGER.debug("Archive entry gone before its purge (entry=%s)", entry.entry_id)
            gone.append(entry.entry_id)
        except ArchiveEntryBusyError as error:
            _LOGGER.debug(
                "Archive entry not claimed for a purge (entry=%s state=%s)",
                entry.entry_id,
                error.state,
            )
            skipped.append(SkippedPurge(entry.entry_id, BUSY, error.state))
        except Exception as error:
            # The claim changed nothing; an entry already ``purging`` stays so.
            failure = _failed(entry.entry_id, error)
            if entry.state == ARCHIVE_STATE_PURGING:
                pending.append(PendingPurge(entry.entry_id, failure))
            else:
                skipped.append(SkippedPurge(entry.entry_id, failure, entry.state))
    if not claimed:
        return PurgeOutcome(pending=tuple(pending), skipped=tuple(skipped), gone=tuple(gone))
    usage_import = ("usage_import", services.data_dir)
    try:
        services.import_usage()
    except Exception as error:
        # Recorded usage must reach the usage ledger before its Sessions go.
        if _FAILURES.started(usage_import, type(error).__name__):
            _LOGGER.warning(
                "Usage import before a purge failed; nothing was purged and the purge is "
                "retried: %s",
                error,
            )
        return PurgeOutcome(
            pending=(
                *pending,
                *(PendingPurge(entry.entry_id, USAGE_IMPORT_FAILED) for entry in claimed),
            ),
            skipped=tuple(skipped),
            gone=tuple(gone),
        )
    if _FAILURES.ended(usage_import):
        _LOGGER.info("Usage import before a purge no longer fails")
    purged: list[PurgedEntry] = []
    for entry in claimed:
        result = _purge_claimed(services, entry, reason=reason, actor=actor, stopping=stopping)
        if isinstance(result, PurgedEntry):
            purged.append(result)
        else:
            pending.append(result)
    return PurgeOutcome(tuple(purged), tuple(pending), tuple(skipped), tuple(gone))


def _purge_claimed(
    services: ArchiveServices,
    entry: ArchiveEntry,
    *,
    reason: str,
    actor: str,
    stopping: threading.Event | None,
) -> PurgedEntry | PendingPurge:
    ledger = services.sessions.archive_ledger
    archive_root = services.data_dir / ARCHIVE_ROOT

    def check_stop() -> None:
        if stopping is not None and stopping.is_set():
            raise _StoppedError

    try:
        check_stop()
        while ledger.purge_next_session(entry.entry_key):
            check_stop()
        for tree in entry.trees:
            check_stop()
            path = stored_path(services, tree.path)
            remove_tree(path, within=archive_root)
            prune_empty_parents(services, path)
        ledger.finish_purge(entry.entry_key)
    except _StoppedError:
        _LOGGER.debug("Archive entry purge stopped; it continues later (entry=%s)", entry.entry_id)
        return PendingPurge(entry.entry_id, STOPPED)
    except Exception as error:
        return PendingPurge(entry.entry_id, _failed(entry.entry_id, error))
    if _FAILURES.ended(entry.entry_id):
        _LOGGER.info("Archive entry purge no longer fails (entry=%s)", entry.entry_id)
    _LOGGER.info(
        "Archive entry purged (entry=%s kind=%s subject=%s sessions=%d reason=%s actor=%s)",
        entry.entry_id,
        entry.kind,
        entry.subject_id,
        entry.session_count,
        reason,
        actor,
    )
    return PurgedEntry(entry.entry_id, entry.kind, entry.subject_id, entry.session_count)


def _failed(entry_id: str, error: Exception) -> str:
    """Log a purge failure once per entry and failure kind; the failure's class."""
    failure = type(error).__name__
    if _FAILURES.started(entry_id, failure):
        _LOGGER.warning(
            "Archive entry could not be purged (entry=%s reason=%s): %s",
            entry_id,
            failure,
            error,
        )
    return failure
