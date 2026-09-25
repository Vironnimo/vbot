"""The data-store marker, the maintenance guard and the data-store operation lock.

``data-store.json`` authorizes the canonical databases of one data directory.
Data-directory initialization writes it, with no databases listed, only when it
creates a genuinely new root. A canonical database is listed once it exists and
has been verified; a listed database that is missing or corrupt is restored from
a data snapshot, or startup fails. A data directory without a marker is not a
current-format store, and Runtime refuses it without inspecting anything else.

``data-maintenance.json`` exists while an offline operation (a converter, a
generation conversion, a restore) is incomplete. Runtime refuses to open any
canonical database while it exists.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.database._connections import readonly_sqlite_uri
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseFormatError,
    DatabaseUnavailableError,
)
from core.database.spec import canonical_database_path, validate_database_name
from core.utils.atomic import atomic_write_text

_LOGGER = logging.getLogger("vbot.database")

MARKER_FILE_NAME = "data-store.json"
MAINTENANCE_GUARD_FILE_NAME = "data-maintenance.json"
OPERATION_LOCK_FILE_NAME = "data-store.lock"
MARKER_FORMAT_VERSION = 1
OPERATION_LOCK_TIMEOUT_SECONDS = 10.0
_MARKER_KEYS = frozenset({"format_version", "databases"})
_ENTRY_KEYS = frozenset({"database_id", "format_generation"})
_DATABASE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MAINTENANCE_KEYS = frozenset({"operation_id", "operation", "started_at", "pid"})


@dataclass(frozen=True)
class MarkerEntry:
    """One registered canonical database."""

    database_id: str
    format_generation: int


@dataclass(frozen=True)
class DataStoreMarker:
    """The strictly validated ``data-store.json`` content."""

    databases: Mapping[str, MarkerEntry]


@dataclass(frozen=True)
class MaintenanceOperation:
    """One incomplete offline operation recorded in ``data-maintenance.json``."""

    operation_id: str
    operation: str
    started_at: str
    pid: int


def marker_path(data_dir: Path) -> Path:
    return Path(data_dir) / MARKER_FILE_NAME


def maintenance_guard_path(data_dir: Path) -> Path:
    return Path(data_dir) / MAINTENANCE_GUARD_FILE_NAME


def new_database_id() -> str:
    """A fresh database identity shared by the marker and ``kernel_meta``."""
    return uuid.uuid4().hex


def valid_database_id(value: object) -> bool:
    return isinstance(value, str) and _DATABASE_ID_PATTERN.fullmatch(value) is not None


def utc_now() -> str:
    """Canonical fixed-width UTC timestamp: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------


def read_marker(data_dir: Path) -> DataStoreMarker | None:
    """Load and strictly validate the marker; ``None`` when it does not exist."""
    path = marker_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeError as exc:
        raise DatabaseFormatError(f"data-store marker is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise DatabaseUnavailableError(f"data-store marker cannot be read: {path}") from exc
    return _parse_marker(raw, path)


def _parse_marker(raw: str, path: Path) -> DataStoreMarker:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatabaseFormatError(f"data-store marker is malformed: {path}") from exc
    if not isinstance(payload, dict) or set(payload) != _MARKER_KEYS:
        raise DatabaseFormatError(f"data-store marker has an unexpected shape: {path}")
    format_version = payload["format_version"]
    if not isinstance(format_version, int) or isinstance(format_version, bool):
        raise DatabaseFormatError(f"data-store marker has an invalid format version: {path}")
    if format_version > MARKER_FORMAT_VERSION:
        raise DatabaseFormatError(
            f"data-store marker is from a newer vBot: format version {format_version} at "
            f"{path} exceeds supported {MARKER_FORMAT_VERSION}"
        )
    if format_version != MARKER_FORMAT_VERSION:
        raise DatabaseFormatError(
            f"data-store marker has an unsupported format version {format_version}: {path}"
        )
    databases = payload["databases"]
    if not isinstance(databases, dict):
        raise DatabaseFormatError(f"data-store marker has an invalid database list: {path}")
    entries: dict[str, MarkerEntry] = {}
    for name, entry in databases.items():
        try:
            validate_database_name(name)
        except ValueError as exc:
            raise DatabaseFormatError(
                f"data-store marker lists an invalid database name {name!r}: {path}"
            ) from exc
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise DatabaseFormatError(f"data-store marker entry {name} has an invalid shape")
        generation = entry["format_generation"]
        if (
            not valid_database_id(entry["database_id"])
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise DatabaseFormatError(f"data-store marker entry {name} is invalid: {path}")
        entries[name] = MarkerEntry(str(entry["database_id"]), generation)
    return DataStoreMarker(entries)


def _write_marker(data_dir: Path, databases: Mapping[str, MarkerEntry]) -> DataStoreMarker:
    path = marker_path(data_dir)
    payload = {
        "format_version": MARKER_FORMAT_VERSION,
        "databases": {
            name: {
                "database_id": entry.database_id,
                "format_generation": entry.format_generation,
            }
            for name, entry in sorted(databases.items())
        },
    }
    try:
        atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        raise DatabaseUnavailableError(f"data-store marker cannot be written: {path}") from exc
    return DataStoreMarker(dict(databases))


def write_bootstrap_marker(data_dir: Path) -> DataStoreMarker:
    """Authorize a new data directory: every canonical database may be created.

    Replaces any existing marker, so callers use it only for a directory they
    just created.
    """
    return _write_marker(Path(data_dir), {})


def register_database(data_dir: Path, name: str, entry: MarkerEntry) -> DataStoreMarker:
    """List one verified canonical database, preserving every other entry."""
    with operation_lock(data_dir):
        return register_database_locked(data_dir, name, entry)


def register_database_locked(data_dir: Path, name: str, entry: MarkerEntry) -> DataStoreMarker:
    """``register_database`` for a caller that already holds the operation lock."""
    marker = read_marker(data_dir)
    if marker is None:
        raise DatabaseFormatError(
            f"the data directory does not authorize a current-format data store: {data_dir}"
        )
    existing = marker.databases.get(name)
    if existing == entry:
        return marker
    if existing is not None:
        raise DatabaseFormatError(
            f"{name} is already registered with a different identity in {data_dir}"
        )
    return _write_marker(data_dir, {**marker.databases, name: entry})


def unregister_databases_locked(data_dir: Path, names: Iterable[str]) -> DataStoreMarker:
    """Remove registrations whose files were already moved to quarantine.

    The caller holds the operation lock; a repeated call after an interruption
    is harmless.
    """
    marker = read_marker(data_dir)
    if marker is None:
        raise DatabaseFormatError(
            f"the data directory does not authorize a current-format data store: {data_dir}"
        )
    retired = set(names)
    if not retired & set(marker.databases):
        return marker
    return _write_marker(
        data_dir,
        {name: entry for name, entry in marker.databases.items() if name not in retired},
    )


def write_marker_for_databases(data_dir: Path, paths: Iterable[Path]) -> DataStoreMarker:
    """Write ``data-store.json`` listing exactly the given canonical database files.

    Each file must sit at the canonical path for the name recorded in its own
    ``kernel_meta``; its identity and format generation come from there. This is
    the offline API for converters and staging directories; the caller owns the
    maintenance guard around it.
    """
    data_dir = Path(data_dir)
    entries: dict[str, MarkerEntry] = {}
    for path in paths:
        identity = read_kernel_identity(Path(path))
        name = identity["database_name"]
        expected = canonical_database_path(data_dir, name).resolve()
        if Path(path).resolve() != expected:
            raise DatabaseFormatError(
                f"{path} records database {name}, whose canonical path is {expected}"
            )
        if name in entries:
            raise DatabaseFormatError(f"database {name} is listed twice")
        entries[name] = MarkerEntry(identity["database_id"], int(identity["format_generation"]))
    with operation_lock(data_dir):
        return _write_marker(data_dir, entries)


def read_kernel_identity(path: Path) -> dict[str, str]:
    """Read ``kernel_meta`` from a closed database file without changing it."""
    try:
        with contextlib.closing(sqlite3.connect(readonly_sqlite_uri(path), uri=True)) as connection:
            generation = int(connection.execute("PRAGMA user_version").fetchone()[0])
            rows = connection.execute("SELECT key, value FROM kernel_meta").fetchall()
    except sqlite3.Error as exc:
        raise DatabaseCorruptError(f"{path} has no readable kernel identity") from exc
    identity = {str(key): str(value) for key, value in rows}
    if (
        not valid_database_id(identity.get("database_id"))
        or identity.get("format_generation") != str(generation)
        or "database_name" not in identity
    ):
        raise DatabaseCorruptError(f"{path} has an invalid kernel identity")
    try:
        validate_database_name(identity["database_name"])
    except ValueError as exc:
        raise DatabaseCorruptError(f"{path} records an invalid database name") from exc
    return identity


# ---------------------------------------------------------------------------
# Maintenance guard
# ---------------------------------------------------------------------------


def read_maintenance(data_dir: Path) -> MaintenanceOperation | None:
    """Return the incomplete offline operation, or ``None``.

    A guard that cannot be parsed still blocks: it is reported with operation
    ``"unknown"``.
    """
    path = maintenance_guard_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatabaseUnavailableError(f"data maintenance guard cannot be read: {path}") from exc
    except UnicodeError:
        raw = ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if (
        not isinstance(payload, dict)
        or not _MAINTENANCE_KEYS.issubset(payload)
        or not isinstance(payload.get("operation_id"), str)
        or not isinstance(payload.get("operation"), str)
        or not isinstance(payload.get("started_at"), str)
        or not isinstance(payload.get("pid"), int)
    ):
        return MaintenanceOperation("", "unknown", "", 0)
    return MaintenanceOperation(
        str(payload["operation_id"]),
        str(payload["operation"]),
        str(payload["started_at"]),
        int(payload["pid"]),
    )


def require_no_maintenance(data_dir: Path) -> None:
    """Refuse while an offline operation is incomplete."""
    operation = read_maintenance(data_dir)
    if operation is not None:
        raise DatabaseFormatError(
            f"data maintenance is incomplete ({operation.operation}"
            + (f", started {operation.started_at}" if operation.started_at else "")
            + f"); resume or finish the offline operation first: "
            f"{maintenance_guard_path(data_dir)}"
        )


def begin_maintenance(
    data_dir: Path, operation: str, *, resume: bool = False
) -> MaintenanceOperation:
    """Publish the guard before an offline operation changes any database file.

    Refuses while any operation is incomplete. With ``resume``, an operation
    may replace a guard left by an interrupted run of the same operation, such
    as a restore repeated after a crash.
    """
    if not operation or not operation.strip():
        raise ValueError("maintenance operation must be named")
    with operation_lock(data_dir):
        current = read_maintenance(data_dir)
        if current is not None and not (resume and current.operation == operation):
            raise DatabaseFormatError(
                f"data maintenance is already incomplete ({current.operation}): "
                f"{maintenance_guard_path(data_dir)}"
            )
        record = MaintenanceOperation(uuid.uuid4().hex, operation, utc_now(), os.getpid())
        payload = {
            "operation_id": record.operation_id,
            "operation": record.operation,
            "started_at": record.started_at,
            "pid": record.pid,
        }
        path = maintenance_guard_path(data_dir)
        try:
            atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            raise DatabaseUnavailableError(
                f"data maintenance guard cannot be written: {path}"
            ) from exc
        return record


def finish_maintenance(data_dir: Path, record: MaintenanceOperation) -> None:
    """Remove the guard once the operation completed; only the owner's guard."""
    with operation_lock(data_dir):
        current = read_maintenance(data_dir)
        if current is None:
            return
        if current.operation_id != record.operation_id:
            raise DatabaseFormatError(
                f"data maintenance guard belongs to another operation ({current.operation})"
            )
        path = maintenance_guard_path(data_dir)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DatabaseUnavailableError(
                f"data maintenance guard cannot be removed: {path}"
            ) from exc


@contextlib.contextmanager
def maintenance(
    data_dir: Path, operation: str, *, resume: bool = False
) -> Iterator[MaintenanceOperation]:
    """Hold the guard for one offline operation.

    The guard is removed only when the block completes. A failure leaves it in
    place, so Runtime keeps refusing the half-finished data directory.
    """
    record = begin_maintenance(data_dir, operation, resume=resume)
    yield record
    finish_maintenance(data_dir, record)


# ---------------------------------------------------------------------------
# Operation lock
# ---------------------------------------------------------------------------


@dataclass
class OperationLock:
    """An OS-owned descriptor lock; the lock file itself is only diagnostic state."""

    path: Path
    fd: int
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            if os.name == "nt":
                import msvcrt

                windows_console: Any = msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                windows_console.locking(self.fd, windows_console.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except (ImportError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                os.close(self.fd)


def operation_lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / OPERATION_LOCK_FILE_NAME


def acquire_operation_lock(
    data_dir: Path,
    timeout: float | None = None,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> OperationLock | None:
    """Acquire the crash-releasing data-store lock, or ``None`` on timeout.

    It serializes marker writes, snapshots, restores and incident writes across
    processes. The file is never removed based on wall-clock age: a process that
    died leaves only a diagnostic file, and the operating system releases the
    descriptor lock.
    """
    lock_path = operation_lock_path(data_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + (OPERATION_LOCK_TIMEOUT_SECONDS if timeout is None else timeout)
    while time.monotonic() < deadline and not (cancelled is not None and cancelled()):
        descriptor: int | None = None
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                windows_console: Any = msvcrt
                windows_console.locking(descriptor, windows_console.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            with contextlib.suppress(OSError):
                os.ftruncate(descriptor, 0)
                os.write(descriptor, f"pid={os.getpid()}".encode())
            return OperationLock(lock_path, descriptor)
        except (ImportError, OSError):
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            time.sleep(0.05)
    return None


@contextlib.contextmanager
def operation_lock(data_dir: Path, timeout: float | None = None) -> Iterator[OperationLock]:
    """Hold the data-store lock; a contended lock is ``DatabaseUnavailableError``."""
    try:
        lock = acquire_operation_lock(data_dir, timeout)
    except OSError as exc:
        raise DatabaseUnavailableError("the data-store operation lock is unavailable") from exc
    if lock is None:
        raise DatabaseUnavailableError("the data-store operation lock is busy")
    try:
        yield lock
    finally:
        lock.release()
