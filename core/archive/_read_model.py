"""How archive entries read: labels, restorability, pages and details."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.agents import AGENT_FORMAT_VERSION
from core.archive import _restore, _retention
from core.archive._operations import stored_path
from core.archive._types import ArchiveEntryDetail, ArchiveListing, ArchivePage
from core.projects import PROJECT_FORMAT_VERSION
from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_OWNER_GROUP,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_KIND_SESSION,
    ARCHIVE_STATE_ARCHIVED,
    ARCHIVE_TREE_AGENT,
    ARCHIVE_TREE_PROJECT,
    ArchiveEntry,
    ArchiveEntryCursor,
    ArchiveEntryFilter,
    ArchiveEntryNotFoundError,
)

if TYPE_CHECKING:
    from core.archive.archive import ArchiveServices

_FORMAT_VERSIONS = {
    ARCHIVE_KIND_AGENT: AGENT_FORMAT_VERSION,
    ARCHIVE_KIND_PROJECT: PROJECT_FORMAT_VERSION,
}
_PAYLOAD_ROLES = {
    ARCHIVE_KIND_AGENT: ARCHIVE_TREE_AGENT,
    ARCHIVE_KIND_PROJECT: ARCHIVE_TREE_PROJECT,
}


def page(
    services: ArchiveServices,
    filters: ArchiveEntryFilter,
    cursor: ArchiveEntryCursor | None,
    limit: int,
) -> ArchivePage:
    entries = services.sessions.archive_ledger.page(filters, cursor=cursor, limit=limit)
    retention_days = services.retention_days()
    return ArchivePage(
        tuple(listing(services, entry, retention_days) for entry in entries.entries),
        entries.next_cursor,
        retention_days,
    )


def show(services: ArchiveServices, entry_id: str, session_limit: int) -> ArchiveEntryDetail:
    ledger = services.sessions.archive_ledger
    entry = ledger.entry(entry_id)
    if entry is None:
        raise ArchiveEntryNotFoundError(entry_id)
    if not entry.trees:
        files = "none"
    elif all(stored_path(services, tree.path).exists() for tree in entry.trees):
        files = "present"
    else:
        files = "missing"
    marked = entry.facts.get("user_folders")
    named = set(marked) if isinstance(marked, list) else set()
    return ArchiveEntryDetail(
        listing=listing(services, entry, services.retention_days()),
        sessions=ledger.members(entry.entry_key, limit=session_limit),
        files=files,
        restore=_restore.check(services, entry_id, None),
        user_folders=tuple(tree for tree in entry.trees if tree.path in named),
    )


def listing(
    services: ArchiveServices, entry: ArchiveEntry, retention_days: int | None
) -> ArchiveListing:
    reason = _not_restorable_reason(services, entry)
    return ArchiveListing(
        entry,
        _label(services, entry),
        reason is None,
        reason,
        _retention.purge_at(entry, retention_days),
    )


def _label(services: ArchiveServices, entry: ArchiveEntry) -> str:
    name = entry.facts.get("name")
    if entry.kind in (ARCHIVE_KIND_AGENT, ARCHIVE_KIND_PROJECT) and isinstance(name, str) and name:
        return name
    if entry.kind == ARCHIVE_KIND_SESSION:
        members = services.sessions.archive_ledger.members(entry.entry_key, limit=1)
        if members and members[0].title:
            return members[0].title
    if entry.kind == ARCHIVE_KIND_OWNER_GROUP and entry.owner_name is not None:
        titles = services.sessions.temporary_group_titles(
            owner_name=entry.owner_name, group_ids=(entry.subject_id,)
        )
        if title := titles.get(entry.subject_id):
            return title
    return entry.subject_id


def _not_restorable_reason(services: ArchiveServices, entry: ArchiveEntry) -> str | None:
    """Why a list already knows an entry cannot be restored, from cheap facts only."""
    if entry.kind not in _restore.RESTORABLE_KINDS:
        return "kind_not_restorable"
    if entry.state != ARCHIVE_STATE_ARCHIVED:
        return "entry_busy"
    if entry.kind not in _FORMAT_VERSIONS:
        return None
    version = _FORMAT_VERSIONS[entry.kind]
    recorded = entry.facts.get("payload_format")
    if recorded == "older" or (
        isinstance(recorded, int) and not isinstance(recorded, bool) and recorded < version
    ):
        return "older_format"
    if recorded == "newer" or (isinstance(recorded, int) and recorded > version):
        return "newer_format"
    if recorded != version:
        return "payload_invalid"
    role = _PAYLOAD_ROLES[entry.kind]
    trees = [tree for tree in entry.trees if tree.role == role]
    if not trees or not all(stored_path(services, tree.path).is_dir() for tree in trees):
        return "payload_missing"
    return None
