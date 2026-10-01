"""The retention period: how long an archive entry rests before it is deleted permanently.

An entry's ``purge_at`` is computed on read from its ``retention_start`` and the
current period, so a changed period applies to existing entries at once.
Retention never deletes ``files`` entries or entries whose ``user_folders``
fact names folders the user may own rather than copies vBot made; only a
manual purge deletes those.
"""

from __future__ import annotations

from datetime import timedelta

from core.sessions import ARCHIVE_KIND_FILES, ARCHIVE_STATE_ARCHIVED, ArchiveEntry
from core.utils.timestamps import format_canonical_timestamp, parse_canonical_timestamp

# The retention period in days unless configured otherwise.
DEFAULT_RETENTION_DAYS = 30


def default_retention_days() -> int | None:
    """The retention period in days; ``None`` keeps entries until they are deleted."""
    return DEFAULT_RETENTION_DAYS


def purge_at(entry: ArchiveEntry, retention_days: int | None) -> str | None:
    """When the retention period of ``entry`` ends, or ``None`` when retention never deletes it.

    Only a resting (``archived``) entry is due; a ``purging`` one is already
    being deleted, and the other states belong to an operation in progress.
    """
    if retention_days is None or entry.state != ARCHIVE_STATE_ARCHIVED:
        return None
    if entry.kind == ARCHIVE_KIND_FILES or entry.facts.get("user_folders"):
        return None
    try:
        start = parse_canonical_timestamp(entry.retention_start)
    except ValueError:
        return None
    return format_canonical_timestamp(start + timedelta(days=retention_days))


__all__ = ["DEFAULT_RETENTION_DAYS", "default_retention_days", "purge_at"]
