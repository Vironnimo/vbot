"""Durable file helpers shared by data snapshots, the document set and recovery."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    """The hex SHA-256 of one file's bytes."""
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def fsync_file(path: Path) -> None:
    """Flush one file's data to disk, a read-only file included."""
    if sys.platform != "win32":
        # POSIX syncs through any descriptor, so no write access is needed.
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    # Windows flushes only through a handle with write access, which a file with
    # the read-only attribute refuses: the attribute is cleared for the flush.
    try:
        _fsync_with_write_access(path)
    except PermissionError:
        mode = os.stat(path).st_mode
        if mode & stat.S_IWRITE:
            raise
        os.chmod(path, mode | stat.S_IWRITE)
        try:
            _fsync_with_write_access(path)
        finally:
            os.chmod(path, mode)


def _fsync_with_write_access(path: Path) -> None:
    # Binary mode: a Windows text-mode open can strip a trailing CTRL-Z.
    with Path(path).open("r+b") as handle:
        os.fsync(handle.fileno())


def fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
