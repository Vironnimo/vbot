"""The archive's part of a ``project.json`` format conversion.

An archived Project keeps its Anchor, ``project.json`` included, under
``archive/`` and records the payload's format version in the entry's facts
(``payload_format``), which decide whether it can be restored. The Projects
converter rewrites the document; this module finds the archived Projects that
still hold version 1 and records the new version once the payload is converted.
Run only where the Projects converter runs: an update's verification start and
``vbot data-store convert``.
"""

from __future__ import annotations

from pathlib import Path

from core.projects import PROJECT_FORMAT_VERSION
from core.projects.conversion import (
    CONVERTED_PROJECT_FORMAT_VERSION,
    convert_archived_project,
    project_file_needs_conversion,
)
from core.sessions import (
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_ROOT,
    ARCHIVE_STATE_ARCHIVED,
    ARCHIVE_TREE_PROJECT,
    ArchiveEntryFilter,
    SessionArchiveLedger,
)


def convert_archived_projects(data_dir: Path, ledger: SessionArchiveLedger) -> tuple[str, ...]:
    """Convert every archived Project recorded with format version 1; return their entry ids.

    Idempotent: the payload is converted before its facts change, so a pass
    interrupted between the two converts nothing again and only records the
    version. Entries that are not at rest (restoring, purging) stay as they are.
    A payload that cannot be read raises ``OSError``.
    """
    converted: list[str] = []
    cursor = None
    while True:
        page = ledger.page(ArchiveEntryFilter(kind=ARCHIVE_KIND_PROJECT), cursor=cursor, limit=100)
        for entry in page.entries:
            recorded = entry.facts.get("payload_format")
            if (
                entry.state != ARCHIVE_STATE_ARCHIVED
                or isinstance(recorded, bool)
                or recorded != CONVERTED_PROJECT_FORMAT_VERSION
            ):
                continue
            for tree in entry.trees:
                if tree.role == ARCHIVE_TREE_PROJECT:
                    anchor = Path(tree.path)
                    convert_archived_project(anchor if anchor.is_absolute() else data_dir / anchor)
            ledger.update_facts(entry.entry_key, {"payload_format": PROJECT_FORMAT_VERSION})
            converted.append(entry.entry_id)
        if page.next_cursor is None:
            return tuple(converted)
        cursor = page.next_cursor


def archived_projects_need_conversion(data_dir: Path) -> bool:
    """Whether a ``project.json`` of version 1 lies anywhere under ``archive/``.

    A check without the Session database, for a caller that must decide before
    it stops the server; a file of another kind with that name only costs a
    conversion pass that changes nothing. Raises ``OSError``.
    """
    root = data_dir / ARCHIVE_ROOT
    return any(project_file_needs_conversion(path) for path in root.rglob("project.json"))
