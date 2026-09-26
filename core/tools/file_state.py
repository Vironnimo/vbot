"""Shared file-mutation coordination for read and apply_patch.

Tracks, per session, the ``(mtime, size)`` of every file a session has read, so
``apply_patch`` Add refuses to clobber a file the session never read or that changed on
disk since it was last read. Update uses the same state only to report that it
merged against newer on-disk content. Per-path locks serialize in-process
mutations, and atomic same-directory replacement prevents partial files on write
failure. Modeled on OpenCode's (since-removed) ``FileTimeService`` for the
session-scoped ``(mtime, size)`` stamps, with no content hashing.

The registry is a single runtime-owned instance injected into the read/apply_patch
tools (constructor injection, like ``ProcessManager`` for ``bash``) — not a module
singleton.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path

from core.utils.logging import get_logger
from core.utils.paths import model_path

_LOGGER = get_logger("tools.file_state")

# Single off-switch for read-stamp tracking. Path locks and atomic writes remain
# active because they protect mutation integrity independently of stale policy.
FILE_STATE_GUARD_ENABLED = True

# Cap on tracked ``(session, path)`` entries so a long-lived server process does
# not grow the map without bound; oldest insertions are evicted first. A rarely
# evicted entry only costs a harmless re-read.
_MAX_TRACKED_FILES = 8192
_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8)


class _ReplaceRetriesExhaustedError(OSError):
    def __init__(self, error: OSError, attempts_made: int):
        # Keep the cause's codes so ``os_error_reason`` can describe it.
        super().__init__(
            error.errno, error.strerror, error.filename, getattr(error, "winerror", None)
        )
        self.attempts_made = attempts_made


class ReadOnlyFileError(PermissionError):
    """Windows refuses to replace a file marked read-only; retrying cannot help."""

    def __init__(self) -> None:
        super().__init__("the file is read-only")


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

        Called for content a Session received in one step, and by ``apply_patch``
        after a successful write — the tool's own write is an implicit read, so the
        next full-file write in the same session needs no re-read.
        """
        stamp = self.stamp(resolved)
        if stamp is not None:
            self.record_stamp(session_id, resolved, stamp)

    def stamp(self, resolved: Path) -> tuple[float, int] | None:
        """Capture a file's ``(mtime, size)`` before a reader consumes its bytes.

        ``read`` records the captured stamp with ``record_stamp`` only after the
        read succeeded: a failed read never counts, while an external write that
        lands during the read leaves the stamp older than the new content, so a
        later full replacement errs toward a harmless re-read.
        """
        if not FILE_STATE_GUARD_ENABLED:
            return None
        return _stamp(resolved)

    def record_stamp(self, session_id: str, resolved: Path, stamp: tuple[float, int]) -> None:
        """Record a stamp captured by ``stamp`` for a completed read."""
        if not FILE_STATE_GUARD_ENABLED:
            return
        key = (session_id, str(resolved))
        with self._stamps_lock:
            # Re-insert so a re-read counts as most-recently-used for eviction.
            self._stamps.pop(key, None)
            self._stamps[key] = stamp
            while len(self._stamps) > _MAX_TRACKED_FILES:
                del self._stamps[next(iter(self._stamps))]

    def check_stale(self, session_id: str, resolved: Path) -> StaleReason | None:
        """Return why a mutation on ``resolved`` is stale, or ``None`` if safe.

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


def _stamp(resolved: Path) -> tuple[float, int] | None:
    """Return a file's ``(mtime, size)``, or ``None`` if it cannot be stat'd."""
    try:
        info = resolved.stat()
    except OSError:
        return None
    return (info.st_mtime, info.st_size)


def atomic_write_bytes(
    resolved: Path,
    payload: bytes,
    *,
    mode: int | None = None,
    before_replace: Callable[[], None] | None = None,
) -> None:
    """Replace ``resolved`` atomically with ``payload``.

    The temporary file lives beside the target so ``os.replace`` stays on one
    filesystem. Existing permission bits are copied before the replace; a new
    file receives the ordinary umask-derived mode. Any failure removes the
    temporary file and leaves the original target intact. An explicit mode
    carries source permissions to a patch move's destination. A read-only target
    on Windows raises ``ReadOnlyFileError`` without retrying, because Windows
    refuses to replace it. With a caller-supplied precondition check, briefly
    retry Windows replace access/sharing failures. Check all caller
    preconditions before every attempt; never replay a write whose replacement
    completed or retry other I/O stages. Exhausted replacement errors carry their
    one-based ``attempts_made`` count.
    """
    target_mode = _existing_mode(resolved)
    if _is_windows_read_only(target_mode):
        raise ReadOnlyFileError()
    final_mode = mode if mode is not None else target_mode

    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary = _create_temporary(resolved.parent)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if final_mode is not None:
            os.chmod(temporary, final_mode)
        for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
            if before_replace is not None:
                before_replace()
            try:
                os.replace(temporary, resolved)
                break
            except OSError as error:
                if _is_windows_read_only(_existing_mode(resolved)):
                    raise ReadOnlyFileError() from error
                if before_replace is None or getattr(error, "winerror", None) not in {5, 32, 33}:
                    raise
                if attempt == len(_REPLACE_RETRY_DELAYS):
                    raise _ReplaceRetriesExhaustedError(error, attempt + 1) from error
                time.sleep(_REPLACE_RETRY_DELAYS[attempt])
        temporary = None
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            _discard_temporary(temporary)
        raise


# Reasons for file-system errors, in English: Windows reports its own messages in
# the system language, and ``str(error)`` includes the absolute path.
_WINDOWS_REASONS = {
    32: "another program is using it",
    33: "another program has locked part of it",
    123: "the path contains characters that are not allowed in file names",
}
_ERRNO_REASONS = {
    errno.EACCES: "permission denied",
    errno.EPERM: "the operation is not permitted",
    errno.ENOENT: "it does not exist",
    errno.EEXIST: "it already exists",
    errno.EISDIR: "it is a directory",
    errno.ENOTDIR: "a part of the path is a file, not a directory",
    errno.ENOSPC: "the disk is full",
    errno.EROFS: "the file system is read-only",
    errno.ENAMETOOLONG: "the path is too long",
    errno.ELOOP: "the path has too many symbolic links",
    errno.EBUSY: "the file is busy",
}


def os_error_reason(error: OSError) -> str:
    """Say why a file operation failed, in English and without the absolute path."""
    if isinstance(error, ReadOnlyFileError):
        return "the file is read-only"
    reason = _WINDOWS_REASONS.get(getattr(error, "winerror", None) or 0)
    if reason is None and error.errno is not None:
        reason = _ERRNO_REASONS.get(error.errno) or os.strerror(error.errno).lower()
    return reason or error.strerror or str(error) or type(error).__name__


def _existing_mode(resolved: Path) -> int | None:
    try:
        return stat.S_IMODE(resolved.stat().st_mode)
    except OSError:
        return None


def _is_windows_read_only(mode: int | None) -> bool:
    return os.name == "nt" and mode is not None and not mode & stat.S_IWRITE


def _create_temporary(directory: Path) -> tuple[int, Path]:
    """Create an exclusive same-directory temporary file for a replacement.

    ``tempfile.mkstemp`` always creates owner-only files. Opening with ``0o666``
    lets the process umask derive the mode a newly created file normally gets.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(100):
        candidate = directory / f".vbot-tmp-{secrets.token_hex(6)}"
        try:
            return os.open(candidate, flags, 0o666), candidate
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "no unused temporary file name", str(directory))


def _discard_temporary(temporary: Path) -> None:
    """Remove a temporary file, clearing a copied Windows read-only attribute."""
    try:
        try:
            temporary.unlink(missing_ok=True)
        except PermissionError:
            os.chmod(temporary, stat.S_IREAD | stat.S_IWRITE)
            temporary.unlink(missing_ok=True)
    except OSError as error:
        _LOGGER.warning("Could not remove temporary file %s: %s", model_path(temporary), error)


__all__ = [
    "FILE_STATE_GUARD_ENABLED",
    "FileReadState",
    "ReadOnlyFileError",
    "StaleReason",
    "atomic_write_bytes",
    "os_error_reason",
]
