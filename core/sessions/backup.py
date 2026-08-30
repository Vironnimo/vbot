"""Rotating best-effort backup snapshots of the canonical Session database."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from core.chat.errors import ChatSessionError

_LOGGER = logging.getLogger("vbot.sessions")

BACKUP_DIRECTORY_NAME = "session-backups"
_BACKUP_FILE_NAME = "sessions-{timestamp}Z.db"
_BACKUP_FILE_PATTERN = re.compile(r"^sessions-(\d{8}T\d{6})Z(?:-\d+)?\.db$")
# One snapshot per server start is the production cadence, so five covers the
# last five starts while bounding the disk cost to roughly five databases.
BACKUP_KEEP_COUNT = 5


def backup_directory(data_dir: Path) -> Path:
    """Return the rotating backup directory under the data directory."""
    return Path(data_dir) / BACKUP_DIRECTORY_NAME


def create_startup_snapshot(
    snapshot: Callable[[Path], None], database_path: Path, data_dir: Path
) -> Path | None:
    """Create one consistent rotating snapshot if the database exists.

    ``snapshot`` writes a consistent copy to the given destination (the
    Session manager's snapshot entry point). Best-effort by contract: a
    failed snapshot is logged and returns ``None`` so a backup problem can
    never block server start. Returns the snapshot path on success, or
    ``None`` for a fresh installation without a database.
    """
    database_path = Path(database_path)
    if not database_path.exists():
        return None
    destination_root = backup_directory(data_dir)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    destination = destination_root / _BACKUP_FILE_NAME.format(timestamp=timestamp)
    # Rapid restarts can snapshot twice within one second; grow a numeric
    # suffix instead of failing the second snapshot.
    suffix = 2
    while destination.exists():
        destination = destination_root / (f"sessions-{timestamp}Z-{suffix}.db")
        suffix += 1
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
        snapshot(destination)
        # Persist the directory entry so a power failure immediately after
        # creation cannot leave the snapshot invisible on Linux filesystems.
        _fsync_directory(destination_root)
    except (ChatSessionError, OSError) as error:
        _LOGGER.warning("Session backup snapshot failed: %s", error)
        return None
    _prune(destination_root)
    _LOGGER.info("Session backup snapshot created at %s", destination)
    return destination


def _prune(destination_root: Path) -> None:
    """Delete the oldest snapshots beyond the retention count."""
    snapshots = sorted(
        (
            (match.group(1), path)
            for path in destination_root.iterdir()
            if (match := _BACKUP_FILE_PATTERN.match(path.name)) is not None
        ),
        reverse=True,
    )
    for _timestamp, path in snapshots[BACKUP_KEEP_COUNT:]:
        try:
            path.unlink()
        except OSError as error:
            _LOGGER.warning("Session backup pruning failed for %s: %s", path, error)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
