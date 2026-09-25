"""Durable file helpers shared by data snapshots, the document set and recovery."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def sha256_file(path: Path) -> str:
    """The hex SHA-256 of one file's bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_file(path: Path) -> None:
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
