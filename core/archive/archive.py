"""The archive: deleted Identity Agents, Projects and Sessions as restorable archive entries.

An archive entry is one archived unit with its Sessions and payload files. The
Session domain keeps the entry rows beside the Sessions they hold
(:class:`~core.sessions.SessionArchiveLedger`); the Agent and Project stores move
their own files (``archive_files``/``restore_files``). This service owns the
policies that compose them: the payload layout ``archive/entries/<entry_id>/``,
the guards around each operation, restore checks, purges, startup recovery and
the owner log lines.

Archiving the same id again creates another entry; no operation replaces or
deletes another entry's payload. Callers hold the automation reference lock
(``AutomationReferences.lock``) across an archive or restore, so the reference
checks here and the change they admit are one step; the Run admission guards are
taken here. Once started, the retention sweep deletes entries whose retention
period has ended (``_retention.py``).
"""

from __future__ import annotations

import builtins
import threading
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from core.archive import _operations, _purge, _read_model, _recovery, _restore
from core.archive._retention import RetentionSweeper, default_retention_days
from core.archive._types import (
    AgentArchiveOutcome,
    ArchiveEntryDetail,
    ArchivePage,
    ProjectArchiveOutcome,
    PurgeOutcome,
    RestoreCheck,
    RestoreOutcome,
    SessionArchiveOutcome,
)
from core.archive.errors import ArchiveSubjectInUseError
from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_KIND_SESSION,
    ARCHIVE_STATE_ARCHIVED,
    ARCHIVE_STATE_PURGING,
    ArchiveEntry,
    ArchiveEntryBusyError,
    ArchiveEntryCursor,
    ArchiveEntryFilter,
    ArchiveEntryNotFoundError,
    SessionAddress,
)
from core.tools.terminal_manager import TerminalOwner
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.agents import AgentStore
    from core.automation import AutomationReferences
    from core.database import SnapshotBarrier
    from core.projects import AgentResolver, ProjectStore
    from core.runs import ChatRunManager
    from core.sessions import ChatSessionManager
    from core.tools.terminal_manager import TerminalManager

_LOGGER = get_logger("archive")

# Purges walk all entries of a filter in pages of this size.
_PURGE_PAGE = 200


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ArchiveServices:
    """The owners an archive operation composes.

    ``agent_references`` names the Channels and live automations that keep an
    Identity Agent from being archived; ``import_usage`` brings the usage ledger
    up to date before a purge; ``invalidate_project`` drops a Project's Team and
    Skill caches; ``retention_days`` reads the current retention period in days
    (``None`` keeps entries until they are deleted); ``clock`` is the current
    UTC time retention compares with.
    """

    data_dir: Path
    sessions: ChatSessionManager
    agents: AgentStore
    projects: ProjectStore
    agent_resolver: AgentResolver
    runs: ChatRunManager
    automation: AutomationReferences
    terminals: TerminalManager
    snapshot_barrier: SnapshotBarrier
    agent_references: Callable[[str], Awaitable[Sequence[str]]]
    import_usage: Callable[[], None]
    remove_agent_from_recall: Callable[[str], Awaitable[None]]
    remove_session_from_recall: Callable[[str, str, str | None], Awaitable[None]]
    invalidate_agent_skills: Callable[[str], None]
    invalidate_project: Callable[[str], None]
    retention_days: Callable[[], int | None] = default_retention_days
    clock: Callable[[], datetime] = _utc_now


class ArchiveService:
    """Archive, restore, purge and read archive entries, and delete them after retention."""

    def __init__(self, services: ArchiveServices) -> None:
        self._services = services
        # Set by stop(): purges end before their next Session or tree.
        self._stopping = threading.Event()
        self._retention = RetentionSweeper(services, self._stopping, self._notify)

    # -- Retention ---------------------------------------------------------------

    def start(self) -> None:
        """Start the retention sweep; call inside the serving Event Loop.

        The first sweep runs a minute later, then one every hour. Never started
        in a safe startup mode.
        """
        self._retention.start()

    def stop(self) -> None:
        """Stop the retention sweep; a purge in progress ends before its next Session."""
        self._retention.stop()

    async def aclose(self) -> None:
        """Stop the retention sweep and wait for the purge in progress to end."""
        await self._retention.aclose()

    def retention_changed(self) -> None:
        """Sweep at once because the retention period changed; any thread."""
        self._retention.wake()

    def add_changed_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` after any archive entry changed; returns its removal."""
        return self._services.sessions.archive_ledger.add_changed_callback(callback)

    # -- Archive -----------------------------------------------------------------

    async def archive_agent(self, agent_id: str, *, actor: str = "rpc") -> AgentArchiveOutcome:
        """Move an Identity Agent with its files and live global Sessions into a new entry.

        Refuses with :class:`ArchiveSubjectInUseError` while a Channel or a live
        automation targets the Agent and with ``RunAdmissionBlockedError`` while
        it has active or queued Runs. A Workspace outside the Agent's directory
        stays where it is. The Agent's id leaves every delegation list.
        """
        services = self._services
        async with services.runs.agent_admission_guard(agent_id, project_id=None):
            references = await services.agent_references(agent_id)
            if references:
                raise ArchiveSubjectInUseError(ARCHIVE_KIND_AGENT, agent_id, references)
            await services.terminals.close_agent_scope(agent_id, None)
            outcome = await services.sessions.run_async(
                _operations.archive_agent, services, agent_id
            )
            # The archive has succeeded; what follows only brings caches, listeners
            # and the Recall index in step, so a failure there never hides it.
            try:
                services.invalidate_agent_skills(agent_id)
            except Exception:
                _LOGGER.exception("Could not drop an archived Agent's Skills (agent=%s)", agent_id)
        self._notify()
        _LOGGER.info(
            "Archive entry created (entry=%s kind=agent subject=%s sessions=%d policies=%d "
            "actor=%s)",
            outcome.entry_id,
            agent_id,
            outcome.session_count,
            len(outcome.policy_agent_ids),
            actor,
        )
        await services.remove_agent_from_recall(agent_id)
        return outcome

    async def archive_project(
        self, project_id: str, *, copy_identity_files: bool = False, actor: str = "rpc"
    ) -> ProjectArchiveOutcome:
        """Move a Project's Anchor and Sessions into a new entry; its repo is never touched.

        Identity Agents rooted in the Project are unrooted first; an Agent whose
        Workspace was elsewhere gets its default Workspace back, copying the
        identity files along with ``copy_identity_files``. Refuses with
        :class:`ArchiveSubjectInUseError` while a live automation targets the
        Project and with ``RunAdmissionBlockedError`` while it has Runs.
        """
        services = self._services
        await services.sessions.run_async(services.projects.get, project_id)
        async with services.runs.project_admission_guard(project_id):
            references = services.automation.project_references(project_id)
            if references:
                raise ArchiveSubjectInUseError(
                    ARCHIVE_KIND_PROJECT,
                    project_id,
                    [reference.label for reference in references],
                    [reference.to_dict() for reference in references],
                )
            await services.terminals.close_project_scope(project_id)
            outcome = await services.sessions.run_async(
                lambda: _operations.archive_project(
                    services, project_id, copy_identity_files=copy_identity_files
                )
            )
        try:
            services.invalidate_project(project_id)
        except Exception:
            _LOGGER.exception(
                "Could not drop an archived Project's caches (project=%s)", project_id
            )
        self._notify()
        _LOGGER.info(
            "Archive entry created (entry=%s kind=project subject=%s sessions=%d "
            "affected_agents=%d actor=%s)",
            outcome.entry_id,
            project_id,
            outcome.session_count,
            len(outcome.affected_agent_ids),
            actor,
        )
        return outcome

    async def archive_session(
        self, address: SessionAddress, *, actor: str = "rpc"
    ) -> SessionArchiveOutcome:
        """Move one live Session into a new entry.

        For an Identity Agent whose current Session it was, the current Session
        moves to the most recently active remaining one, or a new empty one.
        Refuses with :class:`ArchiveSubjectInUseError` while a live automation
        runs in the Session and with ``RunAdmissionBlockedError`` while it has Runs.
        """
        services = self._services
        sessions = services.sessions
        async with services.runs.session_admission_guard(address):
            references = services.automation.session_references(address)
            if references:
                raise ArchiveSubjectInUseError(
                    ARCHIVE_KIND_SESSION,
                    address.session_id,
                    [reference.label for reference in references],
                    [reference.to_dict() for reference in references],
                )
            await sessions.get_async(address)
            was_current = False
            if address.project_id is None:
                was_current = await sessions.run_async(
                    lambda: (
                        services.agents.get(address.agent_id).current_session_id
                        == address.session_id
                    )
                )
            await services.terminals.close_scope(
                TerminalOwner(address.project_id, address.agent_id, address.session_id)
            )
            # Archiving the row and moving the current pointer is one unit for data snapshots.
            async with services.snapshot_barrier.compound_mutation_async():
                ref = await sessions.archive(address)
                next_session_id = None
                if address.project_id is None:
                    agent = await sessions.run_async(
                        services.agents.reset_current_after_session_removed,
                        address.agent_id,
                        address.session_id,
                    )
                    next_session_id = agent.current_session_id
            await services.remove_session_from_recall(
                address.agent_id, address.session_id, address.project_id
            )
        _LOGGER.info(
            "Archive entry created (entry=%s kind=session subject=%s agent=%s project=%s "
            "sessions=1 actor=%s)",
            ref.entry_id,
            address.session_id,
            address.agent_id,
            address.project_id or "-",
            actor,
        )
        return SessionArchiveOutcome(ref.entry_id, address, next_session_id, was_current)

    # -- Restore -----------------------------------------------------------------

    async def restore_check(self, entry_id: str, *, target_id: str | None = None) -> RestoreCheck:
        """What a restore would meet: every blocker and warning at once; changes nothing."""
        return await self._services.sessions.run_async(
            _restore.check, self._services, entry_id, target_id
        )

    async def restore(
        self, entry_id: str, *, target_id: str | None = None, actor: str = "rpc"
    ) -> RestoreOutcome:
        """Bring an entry's Agent, Project or Sessions back, under ``target_id`` when given.

        A taken id or Session address refuses with
        :class:`~core.archive.errors.ArchiveRestoreConflictError`; restoring under
        another id avoids it. Other blockers refuse with
        :class:`~core.archive.errors.ArchiveNotRestorableError`. Under a new id
        the Sessions lose their Channel routing, and delegation grants, roots and
        Sub-Agent links follow the new id. The entry is gone afterwards.
        """
        services = self._services
        entry = await services.sessions.run_async(self._require_entry, entry_id)
        target = entry.subject_id if target_id is None else target_id
        async with await self._restore_guard(entry, target):
            outcome = await services.sessions.run_async(
                _restore.restore, services, entry_id, target_id
            )
            if entry.kind == ARCHIVE_KIND_AGENT:
                services.invalidate_agent_skills(target)
            elif entry.kind == ARCHIVE_KIND_PROJECT:
                services.invalidate_project(target)
        self._notify()
        _LOGGER.info(
            "Archive entry restored (entry=%s kind=%s subject=%s target=%s sessions=%d actor=%s)",
            entry_id,
            entry.kind,
            entry.subject_id,
            target,
            len(outcome.addresses),
            actor,
        )
        return outcome

    async def _restore_guard(
        self, entry: ArchiveEntry, target: str
    ) -> AbstractAsyncContextManager[None]:
        runs = self._services.runs
        if entry.kind == ARCHIVE_KIND_AGENT:
            return runs.agent_admission_guard(target, project_id=None)
        if entry.kind == ARCHIVE_KIND_PROJECT:
            return runs.project_admission_guard(target)
        members = await self._services.sessions.run_async(
            self._services.sessions.archive_ledger.members, entry.entry_key, limit=None
        )
        renamed = target != entry.subject_id
        addresses = [
            SessionAddress(
                member.address.project_id,
                member.address.agent_id,
                target if renamed else member.address.session_id,
            )
            for member in members
        ]
        return runs.session_admission_guard(*addresses)

    # -- Purge -------------------------------------------------------------------

    async def purge(
        self,
        entry_ids: Sequence[str] = (),
        *,
        all_matching: ArchiveEntryFilter | None = None,
        reason: str = "manual",
        actor: str = "rpc",
    ) -> PurgeOutcome:
        """Delete entries permanently: their Sessions, payload trees and the entries.

        Give ``entry_ids`` or ``all_matching``. With ``entry_ids``, an unknown id
        refuses the whole call with ``ArchiveEntryNotFoundError`` and an entry
        that is neither ``archived`` nor ``purging`` with ``ArchiveEntryBusyError``,
        before anything is deleted; ``all_matching`` skips such entries. Recorded
        usage first reaches the usage ledger, so usage totals stay; when that
        fails, nothing is deleted. An entry whose deletion does not finish, because
        of that, a failure partway or :meth:`stop`, is reported pending and stays
        ``purging`` with what is left; the retention sweep continues it, and
        purging it again continues it at once. An entry another operation took
        between that check and the purge is reported skipped (still held, nothing
        deleted) or gone (no longer in the archive).
        """
        services = self._services
        entries = await services.sessions.run_async(self._purge_targets, entry_ids, all_matching)
        if not entries:
            return PurgeOutcome()
        try:
            outcome = await _purge.PURGE_WORKERS.run(
                _purge.purge_entries,
                services,
                entries,
                reason=reason,
                actor=actor,
                stopping=self._stopping,
            )
        finally:
            self._notify()
        if outcome.pending:
            self._retention.wake()
        return outcome

    def _purge_targets(
        self, entry_ids: Sequence[str], all_matching: ArchiveEntryFilter | None
    ) -> builtins.list[ArchiveEntry]:
        ledger = self._services.sessions.archive_ledger
        if all_matching is not None:
            entries: builtins.list[ArchiveEntry] = []
            cursor: ArchiveEntryCursor | None = None
            while True:
                found = ledger.page(all_matching, cursor=cursor, limit=_PURGE_PAGE)
                entries.extend(
                    entry
                    for entry in found.entries
                    if entry.state in (ARCHIVE_STATE_ARCHIVED, ARCHIVE_STATE_PURGING)
                )
                if found.next_cursor is None:
                    return entries
                cursor = found.next_cursor
        found_entries = {entry_id: ledger.entry(entry_id) for entry_id in dict.fromkeys(entry_ids)}
        missing = [entry_id for entry_id, entry in found_entries.items() if entry is None]
        if missing:
            raise ArchiveEntryNotFoundError(*missing)
        resolved = [entry for entry in found_entries.values() if entry is not None]
        for entry in resolved:
            if entry.state not in (ARCHIVE_STATE_ARCHIVED, ARCHIVE_STATE_PURGING):
                raise ArchiveEntryBusyError(entry.entry_id, entry.state)
        return resolved

    # -- Reads -------------------------------------------------------------------

    async def list(
        self,
        filters: ArchiveEntryFilter,
        *,
        cursor: ArchiveEntryCursor | None = None,
        limit: int = 50,
    ) -> ArchivePage:
        """One page of entries, newest first, with the retention period and each ``purge_at``."""
        return await self._services.sessions.run_async(
            _read_model.page, self._services, filters, cursor, limit
        )

    async def show(self, entry_id: str, *, session_limit: int = 100) -> ArchiveEntryDetail:
        """One entry with its first Sessions, payload state and restore check."""
        return await self._services.sessions.run_async(
            _read_model.show, self._services, entry_id, session_limit
        )

    # -- Recovery ----------------------------------------------------------------

    def recover(self) -> None:
        """Finish or undo interrupted archive operations; blocking, for startup.

        Adopts archived Sessions an older vBot left without an entry and orphan
        ``archive/entries/arc_*`` payloads, rolls back interrupted archives and
        restores, and completes their cleanup and follow-up.
        """
        if _recovery.recover(self._services):
            self._notify()

    def _require_entry(self, entry_id: str) -> ArchiveEntry:
        entry = self._services.sessions.archive_ledger.entry(entry_id)
        if entry is None:
            raise ArchiveEntryNotFoundError(entry_id)
        return entry

    def _notify(self) -> None:
        self._services.sessions.archive_ledger.notify_changed()


__all__ = ["ArchiveService", "ArchiveServices"]
