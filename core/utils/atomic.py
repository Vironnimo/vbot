"""Atomic file-write primitives shared across the app.

Every write goes to a unique temp file that is flushed to durable storage and
then moved into place with ``os.replace``, so a reader never observes a
partially written file. On POSIX, the affected directory entries are also
flushed after the replace. The temp file lives adjacent to the target by
default (guaranteeing the same filesystem for the replace); pass ``data_dir``
to stage it under the data directory's canonical atomic-temporary area instead.
Pass ``mode`` (e.g. ``0o600`` for secret control records) to create the temp
file with those exact permissions and re-assert them on the target after the
replace. On failure the temp file is removed and the ``OSError`` re-raised for
the caller to translate into its own domain error.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from core.storage.layout import DataDirectoryLayout


def temporary_path(data_dir: Path, target_path: Path) -> Path:
    """Return a unique canonical temp path for an atomic replace."""

    return DataDirectoryLayout(data_dir).atomic_temporary / f".{target_path.name}.{uuid4().hex}.tmp"


def remove_temporary_file(temp_path: Path) -> None:
    """Best-effort removal of a leftover temporary file."""

    with suppress(OSError):
        temp_path.unlink(missing_ok=True)


def atomic_write_bytes(
    target_path: Path, data: bytes, *, data_dir: Path | None = None, mode: int | None = None
) -> None:
    """Atomically write ``data`` to ``target_path`` (see module docstring)."""

    def write(temp_path: Path) -> None:
        with temp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    _atomic_write(target_path, write, data_dir=data_dir, mode=mode)


def atomic_write_text(
    target_path: Path,
    text: str,
    *,
    data_dir: Path | None = None,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Atomically write ``text`` to ``target_path`` (see module docstring)."""

    def write(temp_path: Path) -> None:
        with temp_path.open("w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

    _atomic_write(target_path, write, data_dir=data_dir, mode=mode)


def _atomic_write(
    target_path: Path,
    write: Callable[[Path], None],
    *,
    data_dir: Path | None,
    mode: int | None = None,
) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if data_dir is not None:
        temp_path = temporary_path(data_dir, target_path)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        temp_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
    try:
        if mode is not None:
            # Create the temp file exclusively with the exact permissions so the
            # replaced result never momentarily carries a wider umask-derived mode.
            descriptor = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
            os.close(descriptor)
        write(temp_path)
        os.replace(temp_path, target_path)
        _fsync_replace_directories(temp_path, target_path)
    except OSError:
        remove_temporary_file(temp_path)
        raise
    if mode is not None:
        # Best-effort exactness after the replace: exotic filesystems may ignore
        # creation modes, but a permission-tightening miss must not fail a write
        # that already succeeded.
        with suppress(OSError):
            os.chmod(target_path, mode)


def _fsync_replace_directories(temp_path: Path, target_path: Path) -> None:
    """Persist the directory entries changed by a successful replace on POSIX."""

    if os.name != "posix":
        return

    _fsync_directory(target_path.parent)
    if temp_path.parent != target_path.parent:
        _fsync_directory(temp_path.parent)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
