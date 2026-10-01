"""The archive ledger: archive entries kept in the Session database.

An archive entry is one archived unit with its Sessions and payload trees.
Because Session states and entry membership live in one database, every step
that moves Sessions into or out of an entry commits together with the entry's
state. The archive domain (``core.archive``) owns the policies and payload
files; this facade owns the rows. All methods are blocking: call them from a
worker thread, never from the Event Loop.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from core.sessions import _store_archive, _store_archive_backfill
from core.sessions._archive_types import (
    ArchiveAdoption,
    ArchiveEntry,
    ArchiveEntryCursor,
    ArchiveEntryFilter,
    ArchiveEntryPage,
    ArchiveEntryRef,
    ArchiveMember,
    ArchiveScope,
    ArchiveTree,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.sessions._types import SessionAddress
    from core.sessions.store import SessionStore

_LOGGER = get_logger("sessions")


class SessionArchiveLedger:
    """Archive entry rows, their Session membership and their payload tree records.

    State transitions are compare-and-set: a call that finds the entry in
    another state raises :class:`~core.sessions.errors.ArchiveEntryBusyError`,
    an unknown entry :class:`~core.sessions.errors.ArchiveEntryNotFoundError`.
    """

    def __init__(self, store: SessionStore) -> None:
        self._store = store
        self._changed_callbacks: list[Callable[[], None]] = []

    def add_changed_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` after archive entries changed; returns its removal."""
        self._changed_callbacks.append(callback)
        return lambda: (
            self._changed_callbacks.remove(callback)
            if callback in self._changed_callbacks
            else None
        )

    def notify_changed(self) -> None:
        for callback in list(self._changed_callbacks):
            try:
                callback()
            except Exception:
                _LOGGER.exception("Archive change callback failed")

    # -- Archive -----------------------------------------------------------------

    def begin(
        self,
        kind: str,
        *,
        subject_id: str,
        project_id: str = "",
        agent_id: str = "",
        facts: Mapping[str, Any] | None = None,
        trees: Callable[[str], Sequence[ArchiveTree]] = lambda _entry_id: (),
    ) -> ArchiveEntryRef:
        """Record an archive about to move files (state ``archiving``).

        ``trees`` receives the new entry id and returns the payload trees the
        archive will create.
        """
        return self._store._execute_write(
            lambda connection: _store_archive.begin(
                connection,
                kind,
                subject_id=subject_id,
                project_id=project_id,
                agent_id=agent_id,
                facts=facts,
                trees=trees,
            )
        )

    def commit_scope(
        self,
        entry_key: int,
        scope: ArchiveScope,
        *,
        facts: Mapping[str, Any] | None = None,
        cleanup_pending: bool = False,
    ) -> int:
        """Archive every live Session of ``scope`` into the entry and mark it ``archived``.

        Returns how many Sessions it archived. Owner-managed Sessions in the
        scope refuse the commit with a ``ChatSessionError``.
        """
        return self._store._execute_write(
            lambda connection: _store_archive.commit_scope(
                connection, entry_key, scope, facts=facts, cleanup_pending=cleanup_pending
            )
        )

    def abandon(self, entry_key: int) -> None:
        """Delete an ``archiving`` entry whose files went back."""
        self._store._execute_write(lambda connection: _store_archive.abandon(connection, entry_key))

    def update_facts(self, entry_key: int, patch: Mapping[str, Any]) -> None:
        """Merge ``patch`` into the entry's facts; other keys stay."""
        self._store._execute_write(
            lambda connection: _store_archive.update_facts(connection, entry_key, patch)
        )

    def finish_cleanup(self, entry_key: int) -> None:
        self._store._execute_write(
            lambda connection: _store_archive.finish_cleanup(connection, entry_key)
        )

    # -- Restore -----------------------------------------------------------------

    def begin_restore(self, entry_id: str, plan: Mapping[str, Any]) -> ArchiveEntry:
        """Claim an ``archived`` entry for a restore (state ``restoring``)."""
        return self._store._execute_write(
            lambda connection: _store_archive.begin_restore(connection, entry_id, plan)
        )

    def commit_restore(
        self,
        entry_key: int,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        strip_channel_keys: bool = False,
    ) -> tuple[SessionAddress, ...]:
        """Make the entry's Sessions live again (state ``restored``); see ``_store_archive``."""
        return self._store._execute_write(
            lambda connection: _store_archive.commit_restore(
                connection,
                entry_key,
                project_id=project_id,
                agent_id=agent_id,
                session_id=session_id,
                strip_channel_keys=strip_channel_keys,
            )
        )

    def abort_restore(self, entry_key: int) -> None:
        self._store._execute_write(
            lambda connection: _store_archive.abort_restore(connection, entry_key)
        )

    def finish_restore(self, entry_key: int) -> None:
        self._store._execute_write(
            lambda connection: _store_archive.finish_restore(connection, entry_key)
        )

    def retarget_subagent_links(
        self,
        *,
        old_project_id: str | None,
        old_agent_id: str,
        new_project_id: str | None,
        new_agent_id: str,
        session_ids: Sequence[str],
    ) -> int:
        """Point live Sub-Agent parent links at restored Sessions under their new scope."""
        return self._store._execute_write(
            lambda connection: _store_archive.retarget_subagent_links(
                connection,
                old_project_id=old_project_id,
                old_agent_id=old_agent_id,
                new_project_id=new_project_id,
                new_agent_id=new_agent_id,
                session_ids=session_ids,
            )
        )

    # -- Purge -------------------------------------------------------------------

    def begin_purge(self, entry_id: str) -> ArchiveEntry:
        """Claim an entry for permanent deletion; a ``purging`` one is resumed."""
        return self._store._execute_write(
            lambda connection: _store_archive.begin_purge(connection, entry_id)
        )

    def purge_next_session(self, entry_key: int) -> bool:
        """Delete one member Session, newest first; ``False`` once none remain."""
        return self._store._execute_write(
            lambda connection: _store_archive.purge_next_session(connection, entry_key)
        )

    def finish_purge(self, entry_key: int) -> None:
        self._store._execute_write(
            lambda connection: _store_archive.finish_purge(connection, entry_key)
        )

    # -- Repair ------------------------------------------------------------------

    def adopt_unrecorded_archived_rows(self) -> ArchiveAdoption:
        """Give archived Sessions that belong to no entry their own entries.

        Only an older vBot that ran after this one writes such rows.
        """
        return self._store._execute_write(_store_archive_backfill.adopt_unrecorded_archived_rows)

    def adopt_payload(
        self,
        entry_id: str,
        kind: str,
        *,
        subject_id: str,
        archived_at: str,
        trees: Sequence[ArchiveTree],
        facts: Mapping[str, Any] | None = None,
    ) -> ArchiveEntryRef:
        """Record an existing payload that has no entry as a recovered entry without Sessions.

        The entry keeps ``entry_id`` when it is free.
        """
        return self._store._execute_write(
            lambda connection: _store_archive.adopt_payload(
                connection,
                entry_id,
                kind,
                subject_id=subject_id,
                archived_at=archived_at,
                trees=trees,
                facts=facts,
            )
        )

    # -- Reads -------------------------------------------------------------------

    def entry(self, entry_id: str) -> ArchiveEntry | None:
        return self._store._read(
            lambda connection: _store_archive.entry_by_id(connection, entry_id)
        )

    def entry_by_key(self, entry_key: int) -> ArchiveEntry | None:
        return self._store._read(
            lambda connection: _store_archive.entry_by_key(connection, entry_key)
        )

    def page(
        self,
        filters: ArchiveEntryFilter,
        *,
        cursor: ArchiveEntryCursor | None = None,
        limit: int = 50,
    ) -> ArchiveEntryPage:
        """One page of entries, newest first."""
        return self._store._read(
            lambda connection: _store_archive.page(connection, filters, cursor, limit)
        )

    def members(self, entry_key: int, *, limit: int | None = 100) -> tuple[ArchiveMember, ...]:
        """The entry's member Sessions in archive order: the first ``limit``, all for ``None``."""
        return self._store._read(
            lambda connection: _store_archive.members(connection, entry_key, limit)
        )

    def unsettled(self) -> tuple[ArchiveEntry, ...]:
        """Entries an interrupted operation left in a transient state or with pending cleanup."""
        return self._store._read(_store_archive.unsettled)

    def due(self, before: str, *, limit: int = 100) -> tuple[ArchiveEntry, ...]:
        """``archived`` entries whose retention clock started at or before ``before``."""
        return self._store._read(lambda connection: _store_archive.due(connection, before, limit))

    def taken_addresses(
        self,
        entry_key: int,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[SessionAddress, ...]:
        """The addresses a restore with these replacements would find held by a live Session.

        The replacements mean what they mean for :meth:`commit_restore`.
        """
        return self._store._read(
            lambda connection: _store_archive.taken_addresses(
                connection,
                entry_key,
                project_id=project_id,
                agent_id=agent_id,
                session_id=session_id,
            )
        )

    def newest_entry_id(self, kind: str, subject_id: str) -> str | None:
        return self._store._read(
            lambda connection: _store_archive.newest_entry_id(connection, kind, subject_id)
        )

    def unrecorded(self, directories: Sequence[str]) -> tuple[str, ...]:
        """The data-dir relative ``directories`` that no entry's payload tree covers or lies in."""
        return self._store._read(
            lambda connection: tuple(
                directory
                for directory in directories
                if not _store_archive.is_recorded(connection, directory)
            )
        )


__all__ = ["SessionArchiveLedger"]
