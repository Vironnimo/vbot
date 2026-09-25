"""Checks that run before a Generation 1 conversion changes anything.

Every refusal leaves the data directory exactly as it was.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

import psutil  # type: ignore[import-untyped]

from core.database import MARKER_FILE_NAME, MaintenanceOperation, read_maintenance
from core.database.errors import DatabaseError
from core.utils.server_control import (
    CONTROL_DIRECTORY_NAME,
    live_server_ports,
    read_server_control,
)
from scripts.converters.persistence_generation_1._context import ConversionError
from scripts.converters.persistence_generation_1._legacy_sqlite import identity
from scripts.converters.persistence_generation_1.sessions import (
    DATABASE as SESSIONS_DATABASE,
)
from scripts.converters.persistence_generation_1.sessions import (
    _has_generation_1_shape,
    _open_source,
)

OPERATION = "generation-1-conversion"
STAGING_DIRECTORY = "generation-1-staging"
BACKUP_DIRECTORY = "pre-generation-1"

# Files whose presence shows a vBot data directory.
_DATA_DIRECTORY_SIGNS = ("settings.json", "sessions.db", "agents", "decisions.db")
# Source databases the conversion rebuilds: the staged copy and its write-ahead
# log both reach about the size of the source before the log is checkpointed.
_REBUILT_DATABASES = ("sessions.db", "decisions.db", "extension-data/swarm/swarm.db")
_REBUILD_FACTOR = 2.5
_OTHER_SOURCES = ("statistics/provider-usage",)
_SPACE_MARGIN = 256 * 1024 * 1024
# A control record names its process by pid and creation time; a reused pid
# has another creation time.
_CREATE_TIME_TOLERANCE = 0.001


class RefusedError(Exception):
    """The data directory cannot be converted now; nothing was changed."""


def require_data_directory(data_dir: Path) -> None:
    if not data_dir.is_dir():
        raise RefusedError(f"{data_dir} is not a directory")
    if not any((data_dir / sign).exists() for sign in _DATA_DIRECTORY_SIGNS):
        raise RefusedError(
            f"{data_dir} does not look like a vBot data directory (no settings.json, "
            "sessions.db or agents)"
        )


def require_no_server(data_dir: Path) -> None:
    """Refuse while any vBot server runs on the data directory, whatever its port."""
    try:
        ports = set(live_server_ports(data_dir))
        ports.update(_ports_of_live_control_records(data_dir))
    except OSError as error:
        raise RefusedError(
            f"the vBot servers running on {data_dir} could not be checked ({error})"
        ) from error
    if ports:
        listed = ", ".join(str(port) for port in sorted(ports))
        raise RefusedError(
            f"a vBot server is running on {data_dir} (port {listed}). Stop vBot completely, "
            "including the desktop and tray application, then run the conversion again"
        )


def _ports_of_live_control_records(data_dir: Path) -> set[int]:
    ports: set[int] = set()
    for path in sorted((data_dir / CONTROL_DIRECTORY_NAME).glob("server-*.json")):
        try:
            port = int(path.stem.removeprefix("server-"))
            record = read_server_control(data_dir, port)
        except ValueError:
            continue
        if record is not None and _process_started_at(record.pid, record.process_create_time):
            ports.add(port)
    return ports


def _process_started_at(pid: int, create_time: float) -> bool:
    try:
        process = psutil.Process(pid)
        return bool(
            abs(process.create_time() - create_time) < _CREATE_TIME_TOLERANCE
            and process.is_running()
        )
    except (OSError, psutil.Error):
        return False


def resumable_guard(data_dir: Path) -> bool:
    """Whether a maintenance guard exists that this conversion may take over.

    The guard of an interrupted conversion is taken over; the guard of another
    offline operation, or of a conversion that is still running, refuses.
    """
    try:
        current = read_maintenance(data_dir)
    except DatabaseError as error:
        raise RefusedError(str(error)) from error
    if current is None:
        return False
    if current.operation != OPERATION:
        raise RefusedError(
            f"another offline data operation is incomplete ({current.operation}); "
            f"finish it first: {data_dir / 'data-maintenance.json'}"
        )
    if _guard_owner_running(current):
        raise RefusedError(f"another Generation 1 conversion is running (process {current.pid})")
    return True


def _guard_owner_running(guard: MaintenanceOperation) -> bool:
    """Whether another process that wrote the guard still runs (not a reused pid)."""
    if guard.pid == os.getpid():
        return False
    try:
        started = dt.datetime.fromisoformat(guard.started_at).timestamp()
        process = psutil.Process(guard.pid)
        return bool(process.is_running() and process.create_time() <= started)
    except (ValueError, OSError, psutil.Error):
        return False


def require_not_converted(data_dir: Path) -> None:
    """Refuse a data directory that already has a data store."""
    if not (data_dir / MARKER_FILE_NAME).exists():
        return
    sessions = data_dir / SESSIONS_DATABASE
    legacy = False
    if sessions.is_file():
        try:
            with _open_source(sessions) as connection:
                identity(connection)
                legacy = not _has_generation_1_shape(connection)
        except ConversionError:
            legacy = True
    if legacy:
        raise RefusedError(
            f"{data_dir} has a data store ({MARKER_FILE_NAME}) but a pre-Generation-1 "
            "sessions.db: an unreleased development build between the database kernel and "
            "Generation 1 wrote it. This shape is not supported"
        )
    raise RefusedError(
        f"{data_dir} is already Generation 1 ({MARKER_FILE_NAME} exists); nothing to convert"
    )


def require_free_backup(data_dir: Path) -> None:
    backup = data_dir / BACKUP_DIRECTORY
    if backup.exists():
        raise RefusedError(
            f"{backup} already exists. Move it out of the data directory before converting, "
            "so the files this conversion replaces cannot mix with older ones"
        )


def require_supported_sources(data_dir: Path, checks: Iterable[Callable[[Path], None]]) -> None:
    for check in checks:
        try:
            check(data_dir)
        except ConversionError as error:
            raise RefusedError(f"unsupported source: {error}") from error


def required_space(data_dir: Path) -> int:
    """The free space a conversion needs on the data directory's volume."""
    rebuilt = sum(tree_size(data_dir / relative) for relative in _REBUILT_DATABASES)
    other = sum(tree_size(data_dir / relative) for relative in _OTHER_SOURCES)
    return int(rebuilt * _REBUILD_FACTOR) + other + _SPACE_MARGIN


def require_space(data_dir: Path) -> tuple[int, int]:
    """Refuse when the volume lacks the space for staging; return (required, free)."""
    required = required_space(data_dir)
    free = shutil.disk_usage(data_dir).free
    if free < required:
        raise RefusedError(
            f"the conversion needs about {format_bytes(required)} of free space on the data "
            f"directory's volume, but only {format_bytes(free)} is free"
        )
    return required, free


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def tree_size(path: Path) -> int:
    """Bytes of a file, or of every file below a directory; 0 when missing."""
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
    return 0
