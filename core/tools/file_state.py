"""Per-session read-before-write / stale-file guard for the write and edit tools.

Tracks, per session, the ``(mtime, size)`` of every file a session has read, so
``write`` and ``edit`` can refuse to clobber a file the session never read or
that changed on disk since it was last read. Modeled on OpenCode's (since-removed)
``FileTimeService``: session-scoped, ``(mtime, size)`` not-equal staleness, a
restamp on the tool's *own* write so repeated edits need no re-read, new files
exempt, and no content hashing (a changed file is detected by metadata only).

The registry is a single runtime-owned instance injected into the read/write/edit
tools (constructor injection, like ``ProcessManager`` for ``bash``) — not a module
singleton.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

# Single off-switch for the whole guard. Default on; set False to disable the
# read-before-write / stale checks process-wide (escape hatch, mainly for tests).
FILE_STATE_GUARD_ENABLED = True

# Cap on tracked ``(session, path)`` entries so a long-lived server process does
# not grow the map without bound; oldest insertions are evicted first. A rarely
# evicted entry only costs a harmless re-read.
_MAX_TRACKED_FILES = 8192


class StaleReason(Enum):
    """Why a write/edit is refused by the read-before-write guard."""

    NEVER_READ = "never_read"
    MODIFIED = "modified"


class FileReadState:
    """Process-wide registry of per-session file read stamps."""

    def __init__(self) -> None:
        self._stamps: dict[tuple[str, str], tuple[float, int]] = {}

    def record_read(self, session_id: str, resolved: Path) -> None:
        """Stamp a file's current ``(mtime, size)`` for a session.

        Called by ``read`` after resolving a file, and by ``write``/``edit`` after
        a successful write — the tool's own write is an implicit read, so the next
        edit in the same session is not flagged as stale and needs no re-read.
        """
        if not FILE_STATE_GUARD_ENABLED:
            return
        stamp = _stamp(resolved)
        if stamp is None:
            return
        key = (session_id, str(resolved))
        # Re-insert so a re-read counts as most-recently-used for eviction.
        self._stamps.pop(key, None)
        self._stamps[key] = stamp
        while len(self._stamps) > _MAX_TRACKED_FILES:
            del self._stamps[next(iter(self._stamps))]

    def check_stale(self, session_id: str, resolved: Path) -> StaleReason | None:
        """Return why a write/edit on ``resolved`` is stale, or ``None`` if safe.

        Only meaningful for a file that exists — the caller skips a non-existent
        write target (a new file is never stale). ``NEVER_READ`` means the session
        has no stamp for the file; ``MODIFIED`` means its current ``(mtime, size)``
        differs from the stamp (changed on disk since the read).
        """
        if not FILE_STATE_GUARD_ENABLED:
            return None
        stamp = self._stamps.get((session_id, str(resolved)))
        if stamp is None:
            return StaleReason.NEVER_READ
        current = _stamp(resolved)
        # A file that vanished between the caller's existence check and here is a
        # race, not staleness; let the write proceed and surface any real error.
        if current is None:
            return None
        if current != stamp:
            return StaleReason.MODIFIED
        return None


def stale_failure_text(reason: StaleReason, resolved: Path) -> tuple[str, str]:
    """Map a stale reason to a ``(failure_code, model-facing message)`` pair.

    Shared by write and edit so the codes and wording stay identical.
    """
    if reason is StaleReason.NEVER_READ:
        return (
            "file_not_read",
            f"{resolved} has not been read in this session. Read it first before writing to it.",
        )
    return (
        "file_modified_since_read",
        f"{resolved} has been modified since you last read it. Read it again before writing to it.",
    )


def _stamp(resolved: Path) -> tuple[float, int] | None:
    """Return a file's ``(mtime, size)``, or ``None`` if it cannot be stat'd."""
    try:
        info = resolved.stat()
    except OSError:
        return None
    return (info.st_mtime, info.st_size)


__all__ = [
    "FILE_STATE_GUARD_ENABLED",
    "FileReadState",
    "StaleReason",
    "stale_failure_text",
]
