"""Atomic file-write primitives shared across the app.

Every write goes to a unique temp file that is then moved into place with
``os.replace``, so a reader never observes a partially written file. The temp
file lives adjacent to the target by default (guaranteeing the same filesystem
for the replace); pass ``data_dir`` to stage it under the data directory's
canonical atomic-temporary area instead. On failure the temp file is removed
and the ``OSError`` re-raised for the caller to translate into its own domain
error.
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


def atomic_write_bytes(target_path: Path, data: bytes, *, data_dir: Path | None = None) -> None:
    """Atomically write ``data`` to ``target_path`` (see module docstring)."""

    def write(temp_path: Path) -> None:
        temp_path.write_bytes(data)

    _atomic_write(target_path, write, data_dir=data_dir)


def atomic_write_text(
    target_path: Path, text: str, *, data_dir: Path | None = None, encoding: str = "utf-8"
) -> None:
    """Atomically write ``text`` to ``target_path`` (see module docstring)."""

    def write(temp_path: Path) -> None:
        temp_path.write_text(text, encoding=encoding)

    _atomic_write(target_path, write, data_dir=data_dir)


def _atomic_write(
    target_path: Path, write: Callable[[Path], None], *, data_dir: Path | None
) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if data_dir is not None:
        temp_path = temporary_path(data_dir, target_path)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        temp_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
    try:
        write(temp_path)
        os.replace(temp_path, target_path)
    except OSError:
        remove_temporary_file(temp_path)
        raise
