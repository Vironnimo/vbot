"""Archive entry records the Session database keeps beside the Sessions they hold.

An archive entry is one archived unit: an Identity Agent, a Project, Sessions,
an Extension group or legacy archive files. Its row, its Session membership and
the archived state of those Sessions always change in one transaction. The
payload trees of an entry are data-dir relative POSIX paths under ``archive/``;
the archive domain (``core.archive``) owns what they hold.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.sessions._types import SessionAddress

ARCHIVE_KIND_AGENT = "agent"
ARCHIVE_KIND_PROJECT = "project"
ARCHIVE_KIND_SESSION = "session"
ARCHIVE_KIND_OWNER_GROUP = "owner_group"
ARCHIVE_KIND_FILES = "files"

# Files may be moving; the Sessions are still live.
ARCHIVE_STATE_ARCHIVING = "archiving"
# The resting state: restorable or purgeable.
ARCHIVE_STATE_ARCHIVED = "archived"
# Files may be moving back; the Sessions are still archived.
ARCHIVE_STATE_RESTORING = "restoring"
# The Sessions are live again; the follow-up still runs before the row goes.
ARCHIVE_STATE_RESTORED = "restored"
# Permanent deletion has begun and resumes until it is complete.
ARCHIVE_STATE_PURGING = "purging"

ARCHIVE_ORIGIN_OPERATION = "operation"
ARCHIVE_ORIGIN_BACKFILL = "backfill"
ARCHIVE_ORIGIN_RECOVERED = "recovered"

# Tree roles: an Agent tree, a moved Workspace, a Project anchor, other files.
ARCHIVE_TREE_AGENT = "agent"
ARCHIVE_TREE_WORKSPACE = "workspace"
ARCHIVE_TREE_PROJECT = "project"
ARCHIVE_TREE_FILES = "files"

# The data-dir relative directory that holds every archive payload.
ARCHIVE_ROOT = "archive"
# The payload directory of entries created by this vBot: archive/entries/<entry_id>/.
ARCHIVE_ENTRIES_DIR = "archive/entries"


@dataclass(frozen=True)
class ArchiveTree:
    """One payload tree of an entry and where it came from."""

    path: str
    role: str
    source_path: str | None = None


@dataclass(frozen=True)
class ArchiveEntryRef:
    """The keys of one archive entry; ``entry_key`` never leaves the Session domain's callers."""

    entry_key: int
    entry_id: str


@dataclass(frozen=True)
class ArchiveEntry:
    """One archive entry row with its trees and its number of member Sessions.

    ``project_id`` and ``agent_id`` are ``""`` when the entry has no such scope.
    ``facts`` is the open facts object; writers merge into it and keep unknown keys.
    """

    entry_key: int
    entry_id: str
    kind: str
    state: str
    subject_id: str
    project_id: str
    agent_id: str
    owner_name: str | None
    archived_at: str
    retention_start: str
    origin: str
    cleanup_pending: bool
    facts: Mapping[str, Any]
    trees: tuple[ArchiveTree, ...]
    session_count: int

    @property
    def ref(self) -> ArchiveEntryRef:
        return ArchiveEntryRef(self.entry_key, self.entry_id)


@dataclass(frozen=True)
class ArchiveMember:
    """One archived Session generation of an entry, for display."""

    address: SessionAddress
    generation_id: str
    title: str | None
    created_at: str
    last_activity_at: str


@dataclass(frozen=True)
class ArchiveEntryFilter:
    """Which entries a page holds.

    ``project_id`` alone selects every entry of that Project (its anchor and its
    Agents' Sessions); ``agent_id`` alone selects the Identity scope of that Agent.
    """

    kind: str | None = None
    agent_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class ArchiveEntryCursor:
    """The position after the last entry of a page, newest first."""

    archived_at: str
    entry_id: str


@dataclass(frozen=True)
class ArchiveEntryPage:
    entries: tuple[ArchiveEntry, ...]
    next_cursor: ArchiveEntryCursor | None


@dataclass(frozen=True)
class ArchiveScope:
    """The live Sessions one scope archive takes: an Identity Agent's or a Project's.

    ``agent_id`` with no ``project_id`` is the Identity scope of that Agent; a
    ``project_id`` with no ``agent_id`` is every Session of that Project.
    """

    project_id: str | None = None
    agent_id: str | None = None


@dataclass(frozen=True)
class ArchiveAdoption:
    """What one adoption of archived rows or legacy files created, per kind."""

    sessions: int = 0
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def entries(self) -> int:
        return sum(self.counts.values())


__all__ = [
    "ARCHIVE_ENTRIES_DIR",
    "ARCHIVE_KIND_AGENT",
    "ARCHIVE_KIND_FILES",
    "ARCHIVE_KIND_OWNER_GROUP",
    "ARCHIVE_KIND_PROJECT",
    "ARCHIVE_KIND_SESSION",
    "ARCHIVE_ORIGIN_BACKFILL",
    "ARCHIVE_ORIGIN_OPERATION",
    "ARCHIVE_ORIGIN_RECOVERED",
    "ARCHIVE_ROOT",
    "ARCHIVE_STATE_ARCHIVED",
    "ARCHIVE_STATE_ARCHIVING",
    "ARCHIVE_STATE_PURGING",
    "ARCHIVE_STATE_RESTORED",
    "ARCHIVE_STATE_RESTORING",
    "ARCHIVE_TREE_AGENT",
    "ARCHIVE_TREE_FILES",
    "ARCHIVE_TREE_PROJECT",
    "ARCHIVE_TREE_WORKSPACE",
    "ArchiveAdoption",
    "ArchiveEntry",
    "ArchiveEntryCursor",
    "ArchiveEntryFilter",
    "ArchiveEntryPage",
    "ArchiveEntryRef",
    "ArchiveMember",
    "ArchiveScope",
    "ArchiveTree",
]
