"""Permanent deletion ("purge") of archive entries.

Every purge runs on the one ``archive`` worker, so two purges never overlap in
this process; the entry's compare-and-set state keeps restores out. A purge
first brings the usage ledger up to date (recorded usage outlives the Sessions),
then deletes the member Sessions newest first, one transaction each, then the
payload trees, then the entry. It holds no compound mutation: an entry left
``purging`` by a failure or a crash keeps what is not deleted yet and a later
purge of it continues.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.archive._operations import prune_empty_parents, stored_path
from core.archive._types import PendingPurge, PurgedEntry, PurgeOutcome
from core.sessions import ARCHIVE_ROOT, ArchiveEntry
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.tree_move import remove_tree
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.archive.archive import ArchiveServices

_LOGGER = get_logger("archive")

PURGE_WORKERS = BoundedWorkerPool(name="archive", max_workers=1)

# One WARNING per entry and failure kind until that entry is purged.
_FAILURES = LoggedConditions()


def purge_entries(
    services: ArchiveServices, entries: Sequence[ArchiveEntry], *, reason: str, actor: str
) -> PurgeOutcome:
    """Purge ``entries`` one after another; a failed one is reported pending, the rest go on."""
    try:
        services.import_usage()
    except Exception as error:
        # Recorded usage must reach the usage ledger before its Sessions go.
        _LOGGER.warning("Usage import before a purge failed; nothing was purged: %s", error)
        return PurgeOutcome(
            pending=tuple(PendingPurge(entry.entry_id, "usage_import_failed") for entry in entries)
        )
    purged: list[PurgedEntry] = []
    pending: list[PendingPurge] = []
    for entry in entries:
        result = _purge_entry(services, entry, reason=reason, actor=actor)
        if isinstance(result, PurgedEntry):
            purged.append(result)
        else:
            pending.append(result)
    return PurgeOutcome(tuple(purged), tuple(pending))


def _purge_entry(
    services: ArchiveServices, entry: ArchiveEntry, *, reason: str, actor: str
) -> PurgedEntry | PendingPurge:
    ledger = services.sessions.archive_ledger
    archive_root = services.data_dir / ARCHIVE_ROOT
    try:
        claimed = ledger.begin_purge(entry.entry_id)
        while ledger.purge_next_session(claimed.entry_key):
            pass
        for tree in claimed.trees:
            path = stored_path(services, tree.path)
            remove_tree(path, within=archive_root)
            prune_empty_parents(services, path)
        ledger.finish_purge(claimed.entry_key)
    except Exception as error:
        failure = type(error).__name__
        if _FAILURES.started(entry.entry_id, failure):
            _LOGGER.warning(
                "Archive entry could not be purged; it is retried (entry=%s reason=%s): %s",
                entry.entry_id,
                failure,
                error,
            )
        return PendingPurge(entry.entry_id, failure)
    if _FAILURES.ended(entry.entry_id):
        _LOGGER.info("Archive entry purge no longer fails (entry=%s)", entry.entry_id)
    _LOGGER.info(
        "Archive entry purged (entry=%s kind=%s subject=%s sessions=%d reason=%s actor=%s)",
        entry.entry_id,
        entry.kind,
        entry.subject_id,
        claimed.session_count,
        reason,
        actor,
    )
    return PurgedEntry(entry.entry_id, entry.kind, entry.subject_id, claimed.session_count)
