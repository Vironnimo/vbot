"""Shared file-mutation coordination for the write and edit tools.

Tracks, per session, the ``(mtime, size)`` of every file a session has read, so
``write`` can refuse to clobber a file the session never read or that changed on
disk since it was last read. ``edit`` uses the same state only to report that it
merged against newer on-disk content. Per-path locks serialize in-process
mutations, and atomic same-directory replacement prevents partial files on write
failure. Modeled on OpenCode's (since-removed) ``FileTimeService`` for the
session-scoped ``(mtime, size)`` stamps, with no content hashing.

The registry is a single runtime-owned instance injected into the read/write/edit
tools (constructor injection, like ``ProcessManager`` for ``bash``) — not a module
singleton.
"""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from enum import Enum
from pathlib import Path

from core.utils.paths import model_path

# Single off-switch for read-stamp tracking. Path locks and atomic writes remain
# active because they protect mutation integrity independently of stale policy.
FILE_STATE_GUARD_ENABLED = True

# Cap on tracked ``(session, path)`` entries so a long-lived server process does
# not grow the map without bound; oldest insertions are evicted first. A rarely
# evicted entry only costs a harmless re-read.
_MAX_TRACKED_FILES = 8192


class StaleReason(Enum):
    """Why a path is stale relative to one Session's last read."""

    NEVER_READ = "never_read"
    MODIFIED = "modified"


class FileReadState:
    """Process-wide registry of read stamps and per-path mutation locks."""

    def __init__(self) -> None:
        self._stamps: dict[tuple[str, str], tuple[float, int]] = {}
        self._stamps_lock = threading.Lock()
        self._path_locks: dict[str, _PathLockEntry] = {}
        self._path_locks_lock = threading.Lock()

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
        with self._stamps_lock:
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
        with self._stamps_lock:
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

    @contextmanager
    def lock_path(self, resolved: Path) -> Iterator[None]:
        """Serialize in-process mutations of one resolved filesystem path.

        Entries are reference-counted and removed after the final waiter leaves,
        so a long-lived Runtime does not retain every path ever mutated.
        """
        key = str(resolved)
        with self._path_locks_lock:
            entry = self._path_locks.get(key)
            if entry is None:
                entry = _PathLockEntry()
                self._path_locks[key] = entry
            entry.users += 1

        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._path_locks_lock:
                entry.users -= 1
                if entry.users == 0 and self._path_locks.get(key) is entry:
                    del self._path_locks[key]


class _PathLockEntry:
    """One ephemeral per-path lock plus its holder/waiter count."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


def stale_failure_text(reason: StaleReason, resolved: Path) -> tuple[str, str]:
    """Map a full-write conflict to a model-facing failure code and message."""
    if reason is StaleReason.NEVER_READ:
        return (
            "file_not_read",
            f"{model_path(resolved)} has not been read in this session. "
            "Read it first before writing to it.",
        )
    return (
        "file_modified_since_read",
        f"{model_path(resolved)} has been modified since you last read it. "
        "Read it again before writing to it.",
    )


def _stamp(resolved: Path) -> tuple[float, int] | None:
    """Return a file's ``(mtime, size)``, or ``None`` if it cannot be stat'd."""
    try:
        info = resolved.stat()
    except OSError:
        return None
    return (info.st_mtime, info.st_size)


def atomic_write_bytes(resolved: Path, payload: bytes) -> None:
    """Replace ``resolved`` atomically with ``payload``.

    The temporary file lives beside the target so ``os.replace`` stays on one
    filesystem. Existing permission bits are copied before the replace. Any
    failure removes the temporary file and leaves the original target intact.
    """
    existing_mode: int | None = None
    with suppress(OSError):
        existing_mode = stat.S_IMODE(resolved.stat().st_mode)

    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".vbot-tmp-",
            dir=resolved.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(temporary, existing_mode)
        os.replace(temporary, resolved)
        temporary = None
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()
        raise


__all__ = [
    "FILE_STATE_GUARD_ENABLED",
    "FileReadState",
    "StaleReason",
    "atomic_write_bytes",
    "stale_failure_text",
]
