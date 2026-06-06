"""Atomic file-write primitives shared across the storage package.

Writes go to a unique temp file under ``<data_dir>/.tmp`` and are then moved into
place with ``os.replace`` so readers never observe a partially written file.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from uuid import uuid4


def temporary_path(data_dir: Path, target_path: Path) -> Path:
    """Return a unique temp path under ``<data_dir>/.tmp`` for an atomic replace."""

    return data_dir / ".tmp" / f".{target_path.name}.{uuid4().hex}.tmp"


def remove_temporary_file(temp_path: Path) -> None:
    """Best-effort removal of a leftover temporary file."""

    with suppress(OSError):
        temp_path.unlink(missing_ok=True)
