"""Verified snapshots, recovery, and health state for the canonical Session store.

This module owns current-format SQLite snapshots only. It deliberately has no
knowledge of legacy Session artifacts or conversion.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.sessions.errors import (
    QuarantineResult,
    SessionRecoveryConflictError,
    SessionStoreCorruptError,
    SessionStoreUnavailableError,
)
from core.sessions.schema import APPLICATION_ID, DATABASE_ID_META_KEY, SCHEMA_VERSION
from core.sessions.sqlite_runtime import readonly_sqlite_uri

SNAPSHOT_ROOT_NAME = "session-snapshots"
SNAPSHOT_MANIFEST_NAME = "manifest.json"
SNAPSHOT_DATABASE_NAME = "sessions.db"
SNAPSHOT_LOCK_NAME = ".lock"
SNAPSHOT_PARTIAL_SUFFIX = ".partial"
SNAPSHOT_HEALTH_FILE = "session-snapshot-health.json"
SNAPSHOT_KEEP_COUNT = 5
SNAPSHOT_KEEP_BYTES = 512 * 1024 * 1024
SNAPSHOT_RESERVE_BYTES = 64 * 1024 * 1024
SNAPSHOT_LOCK_TIMEOUT_SECONDS = 10.0
MANIFEST_VERSION = 1
_SNAPSHOT_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DATABASE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class SnapshotManifest:
    """Strict metadata describing one complete, verified snapshot."""

    manifest_version: int
    snapshot_id: str
    reason: str
    created_at: str
    database_id: str
    schema_version: int
    application_id: int
    sqlite_version: str
    sqlite_source_id: str
    file_size: int
    sha256: str
    session_count: int
    message_count: int
    latest_history_revision: int
    latest_state_revision: int
    integrity: str
    foreign_key_check: str
    database_file: str
    complete: bool


@dataclass(frozen=True)
class _SnapshotVerification:
    database_id: str
    session_count: int
    message_count: int
    latest_history_revision: int
    latest_state_revision: int


@dataclass(frozen=True)
class _SnapshotInventoryEntry:
    path: Path
    manifest: SnapshotManifest


@dataclass
class _OperationLock:
    """An OS-owned lock handle; the lock file itself is only diagnostic state."""

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

                os.lseek(self.fd, 0, os.SEEK_SET)
                windows_console: Any = msvcrt
                windows_console.locking(self.fd, windows_console.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except (ImportError, OSError):
            pass
        finally:
            with suppress(OSError):
                os.close(self.fd)


@dataclass(frozen=True)
class _CanonicalProbe:
    usable: bool
    recoverable: bool
    cause: str
    failure_detected_at: str


class _SnapshotCancelledError(Exception):
    """Internal cooperative stop signal for an unpublished snapshot."""


def snapshot_root(data_dir: Path) -> Path:
    return Path(data_dir) / SNAPSHOT_ROOT_NAME


def _snapshot_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{timestamp}Z-{uuid.uuid4().hex[:8]}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _snapshot_health_path(data_dir: Path) -> Path:
    return Path(data_dir) / SNAPSHOT_HEALTH_FILE


def _record_snapshot_health(
    data_dir: Path, state: str, *, reason: str | None = None, snapshot_id: str | None = None
) -> None:
    path = _snapshot_health_path(data_dir)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = {
        "state": state,
        "reason": reason,
        "snapshot_id": snapshot_id,
        "observed_at": _utc_now(),
    }
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _fsync_file(temporary)
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except OSError:
        with suppress(OSError):
            temporary.unlink()


def read_snapshot_health(data_dir: Path) -> dict[str, Any]:
    """Return durable snapshot-attempt health without exposing Session content."""

    try:
        payload = json.loads(_snapshot_health_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"state": "unknown", "reason": None, "snapshot_id": None, "observed_at": None}
    if not isinstance(payload, dict) or payload.get("state") not in {"healthy", "degraded"}:
        return {
            "state": "degraded",
            "reason": "snapshot health record is malformed",
            "snapshot_id": None,
            "observed_at": None,
        }
    return payload


def _sha256(path: Path, *, cancelled: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if cancelled is not None and cancelled():
                raise _SnapshotCancelledError
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _acquire_lock(
    lock_path: Path,
    timeout: float = 10.0,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> _OperationLock | None:
    """Acquire a crash-releasing POSIX or Windows descriptor lock.

    The file is never removed based on wall-clock age. A process that died
    leaves only a diagnostic file; the operating system releases the descriptor
    lock and a later owner can acquire the same inode.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
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
            with suppress(OSError):
                os.ftruncate(descriptor, 0)
                os.write(descriptor, f"pid={os.getpid()}".encode())
            return _OperationLock(lock_path, descriptor)
        except (ImportError, OSError):
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            time.sleep(0.05)
    return None


def _release_lock(lock: _OperationLock) -> None:
    lock.release()


def _valid_database_id(value: object) -> bool:
    return isinstance(value, str) and _DATABASE_ID_PATTERN.fullmatch(value) is not None


def _parse_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SessionStoreCorruptError(f"snapshot manifest has an invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SessionStoreCorruptError(f"snapshot manifest has an invalid {field}") from exc
    if parsed.tzinfo is None:
        raise SessionStoreCorruptError(f"snapshot manifest has an unzoned {field}")
    return value


_MANIFEST_KEYS = frozenset(
    {
        "manifest_version",
        "snapshot_id",
        "reason",
        "created_at",
        "database_id",
        "schema_version",
        "application_id",
        "sqlite_version",
        "sqlite_source_id",
        "file_size",
        "sha256",
        "session_count",
        "message_count",
        "latest_history_revision",
        "latest_state_revision",
        "integrity",
        "foreign_key_check",
        "database_file",
        "complete",
    }
)


def _parse_manifest(payload: object, *, child_name: str) -> SnapshotManifest:
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_KEYS:
        raise SessionStoreCorruptError("snapshot manifest has an unexpected shape")
    integer_fields = (
        "manifest_version",
        "schema_version",
        "application_id",
        "file_size",
        "session_count",
        "message_count",
        "latest_history_revision",
        "latest_state_revision",
    )
    for field in integer_fields:
        value = payload[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SessionStoreCorruptError(f"snapshot manifest has an invalid {field}")
    if payload["manifest_version"] != MANIFEST_VERSION:
        raise SessionStoreCorruptError("snapshot manifest version is unsupported")
    snapshot_id = payload["snapshot_id"]
    if (
        not isinstance(snapshot_id, str)
        or snapshot_id != child_name
        or _SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None
    ):
        raise SessionStoreCorruptError("snapshot manifest has an invalid snapshot_id")
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise SessionStoreCorruptError("snapshot manifest has an invalid reason")
    created_at = _parse_timestamp(payload["created_at"], field="created_at")
    database_id = payload["database_id"]
    if not _valid_database_id(database_id):
        raise SessionStoreCorruptError("snapshot manifest has an invalid database_id")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise SessionStoreCorruptError("snapshot manifest has an unsupported schema_version")
    if payload["application_id"] != APPLICATION_ID:
        raise SessionStoreCorruptError("snapshot manifest has an unsupported application_id")
    sqlite_version = payload["sqlite_version"]
    sqlite_source_id = payload["sqlite_source_id"]
    if not isinstance(sqlite_version, str) or not sqlite_version:
        raise SessionStoreCorruptError("snapshot manifest has an invalid sqlite_version")
    if not isinstance(sqlite_source_id, str) or not sqlite_source_id:
        raise SessionStoreCorruptError("snapshot manifest has an invalid sqlite_source_id")
    sha256 = payload["sha256"]
    if not isinstance(sha256, str) or _HEX64_PATTERN.fullmatch(sha256) is None:
        raise SessionStoreCorruptError("snapshot manifest has an invalid sha256")
    if payload["integrity"] != "ok" or payload["foreign_key_check"] != "ok":
        raise SessionStoreCorruptError("snapshot manifest does not record verified integrity")
    if payload["database_file"] != SNAPSHOT_DATABASE_NAME or payload["complete"] is not True:
        raise SessionStoreCorruptError("snapshot manifest is incomplete")
    return SnapshotManifest(
        manifest_version=payload["manifest_version"],
        snapshot_id=snapshot_id,
        reason=reason,
        created_at=created_at,
        database_id=database_id,
        schema_version=payload["schema_version"],
        application_id=payload["application_id"],
        sqlite_version=sqlite_version,
        sqlite_source_id=sqlite_source_id,
        file_size=payload["file_size"],
        sha256=sha256,
        session_count=payload["session_count"],
        message_count=payload["message_count"],
        latest_history_revision=payload["latest_history_revision"],
        latest_state_revision=payload["latest_state_revision"],
        integrity=payload["integrity"],
        foreign_key_check=payload["foreign_key_check"],
        database_file=payload["database_file"],
        complete=payload["complete"],
    )


def _safe_snapshot_paths(data_dir: Path, snapshot_dir: Path) -> tuple[Path, Path, Path] | None:
    root = snapshot_root(data_dir)
    candidate = Path(snapshot_dir)
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve()
    except OSError:
        return None
    if candidate.is_symlink() or candidate_resolved.parent != root_resolved:
        return None
    if candidate.name.startswith(".") or candidate.parent.resolve() != root_resolved:
        return None
    manifest_path = candidate / SNAPSHOT_MANIFEST_NAME
    database_path = candidate / SNAPSHOT_DATABASE_NAME
    if (
        manifest_path.is_symlink()
        or database_path.is_symlink()
        or manifest_path.resolve().parent != candidate_resolved
        or database_path.resolve().parent != candidate_resolved
    ):
        return None
    return candidate, manifest_path, database_path


def _read_manifest(snapshot_dir: Path, data_dir: Path) -> tuple[SnapshotManifest, Path] | None:
    paths = _safe_snapshot_paths(data_dir, snapshot_dir)
    if paths is None:
        return None
    candidate, manifest_path, database_path = paths
    if not manifest_path.is_file() or not database_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return _parse_manifest(payload, child_name=candidate.name), database_path
    except (OSError, UnicodeError, json.JSONDecodeError, SessionStoreCorruptError):
        return None


def _verify_snapshot_db(
    path: Path,
    expected_database_id: str | None = None,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> _SnapshotVerification:
    """Verify one standalone current-format database and close it on all paths."""

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(readonly_sqlite_uri(path), uri=True)
        if cancelled is not None:
            connection.set_progress_handler(lambda: int(cancelled()), 10_000)
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if application_id != APPLICATION_ID:
            raise SessionStoreCorruptError("snapshot application_id mismatch")
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version != SCHEMA_VERSION:
            raise SessionStoreCorruptError("snapshot schema version mismatch")
        row = connection.execute(
            "SELECT value FROM store_meta WHERE key = ?", (DATABASE_ID_META_KEY,)
        ).fetchone()
        if row is None:
            raise SessionStoreCorruptError("snapshot database_id is missing or malformed")
        database_id = str(row[0])
        if not _valid_database_id(database_id):
            raise SessionStoreCorruptError("snapshot database_id is missing or malformed")
        if expected_database_id is not None and database_id != expected_database_id:
            raise SessionStoreCorruptError("snapshot database_id mismatch")
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise SessionStoreCorruptError(f"snapshot integrity failed: {integrity}")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SessionStoreCorruptError("snapshot foreign_key_check failed")
        session_count = int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        message_count = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        latest_history = int(
            connection.execute(
                "SELECT COALESCE(MAX(history_revision), 0) FROM sessions"
            ).fetchone()[0]
        )
        latest_state = int(
            connection.execute("SELECT COALESCE(MAX(state_revision), 0) FROM sessions").fetchone()[
                0
            ]
        )
        return _SnapshotVerification(
            database_id=database_id,
            session_count=session_count,
            message_count=message_count,
            latest_history_revision=latest_history,
            latest_state_revision=latest_state,
        )
    except sqlite3.Error as exc:
        if cancelled is not None and cancelled():
            raise _SnapshotCancelledError from exc
        raise SessionStoreCorruptError("snapshot database verification failed") from exc
    finally:
        if connection is not None:
            with suppress(BaseException):
                connection.close()


def _read_database_identity(path: Path) -> str | None:
    try:
        verification = _verify_snapshot_db(path)
    except SessionStoreCorruptError:
        return None
    return verification.database_id


def _write_manifest(path: Path, manifest: SnapshotManifest) -> None:
    payload = {
        "manifest_version": manifest.manifest_version,
        "snapshot_id": manifest.snapshot_id,
        "reason": manifest.reason,
        "created_at": manifest.created_at,
        "database_id": manifest.database_id,
        "schema_version": manifest.schema_version,
        "application_id": manifest.application_id,
        "sqlite_version": manifest.sqlite_version,
        "sqlite_source_id": manifest.sqlite_source_id,
        "file_size": manifest.file_size,
        "sha256": manifest.sha256,
        "session_count": manifest.session_count,
        "message_count": manifest.message_count,
        "latest_history_revision": manifest.latest_history_revision,
        "latest_state_revision": manifest.latest_state_revision,
        "integrity": manifest.integrity,
        "foreign_key_check": manifest.foreign_key_check,
        "database_file": manifest.database_file,
        "complete": manifest.complete,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _fsync_file(path)


def create_snapshot(
    data_dir: Path,
    database_path: Path,
    backup_fn,
    *,
    database_id: str | None = None,
    reason: str = "scheduled",
    cancelled: Callable[[], bool] | None = None,
) -> Path | None:
    """Capture, verify, and atomically publish one snapshot."""

    root = snapshot_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = _acquire_lock(root / SNAPSHOT_LOCK_NAME, cancelled=cancelled)
    if lock is None:
        return None
    partial: Path | None = None
    try:
        if cancelled is not None and cancelled():
            raise _SnapshotCancelledError
        try:
            free_space = __import__("shutil").disk_usage(root).free
            database_size = database_path.stat().st_size if database_path.exists() else 0
            if free_space < database_size + SNAPSHOT_RESERVE_BYTES:
                _record_snapshot_health(
                    data_dir, "degraded", reason="insufficient snapshot reserve"
                )
                return None
        except OSError:
            _record_snapshot_health(data_dir, "degraded", reason="snapshot capacity probe failed")
            return None
        resolved_database_id = database_id or _read_database_identity(database_path)
        if resolved_database_id is None:
            _record_snapshot_health(data_dir, "degraded", reason="database identity is unavailable")
            return None
        snapshot_id = _snapshot_id()
        partial = root / f".{snapshot_id}.{os.getpid()}{SNAPSHOT_PARTIAL_SUFFIX}"
        partial.mkdir(parents=False, exist_ok=False)
        database_destination = partial / SNAPSHOT_DATABASE_NAME
        if backup_fn(database_destination) is False:
            raise _SnapshotCancelledError
        if cancelled is not None and cancelled():
            raise _SnapshotCancelledError
        file_size = database_destination.stat().st_size
        digest = _sha256(database_destination, cancelled=cancelled)
        verification = _verify_snapshot_db(
            database_destination,
            expected_database_id=resolved_database_id,
            cancelled=cancelled,
        )
        from core.sessions.sqlite_runtime import sqlite_source_id

        manifest = SnapshotManifest(
            manifest_version=MANIFEST_VERSION,
            snapshot_id=snapshot_id,
            reason=reason,
            created_at=_utc_now(),
            database_id=verification.database_id,
            schema_version=SCHEMA_VERSION,
            application_id=APPLICATION_ID,
            sqlite_version=sqlite3.sqlite_version,
            sqlite_source_id=sqlite_source_id(),
            file_size=file_size,
            sha256=digest,
            session_count=verification.session_count,
            message_count=verification.message_count,
            latest_history_revision=verification.latest_history_revision,
            latest_state_revision=verification.latest_state_revision,
            integrity="ok",
            foreign_key_check="ok",
            database_file=SNAPSHOT_DATABASE_NAME,
            complete=True,
        )
        _write_manifest(partial / SNAPSHOT_MANIFEST_NAME, manifest)
        _fsync_file(database_destination)
        _fsync_dir(partial)
        if cancelled is not None and cancelled():
            raise _SnapshotCancelledError
        final = root / snapshot_id
        os.replace(partial, final)
        partial = None
        _fsync_dir(root)
        _prune_snapshots(root, protected_snapshot=final)
        _record_snapshot_health(data_dir, "healthy", snapshot_id=snapshot_id)
        return final
    except _SnapshotCancelledError:
        if partial is not None:
            with suppress(OSError):
                __import__("shutil").rmtree(partial, ignore_errors=True)
        return None
    except (OSError, sqlite3.Error, SessionStoreCorruptError, SessionStoreUnavailableError) as exc:
        if partial is not None:
            with suppress(OSError):
                __import__("shutil").rmtree(partial, ignore_errors=True)
        _record_snapshot_health(
            data_dir,
            "degraded",
            reason=f"{type(exc).__name__}: {exc}",
        )
        return None
    finally:
        _release_lock(lock)


def list_snapshots(
    data_dir: Path,
    *,
    expected_database_id: str | None = None,
) -> list[Path]:
    """Return only fixed-root, strict-manifest, hash- and DB-verified snapshots."""

    root = snapshot_root(data_dir)
    if not root.is_dir():
        return []
    verified: list[tuple[str, Path]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir() or child.is_symlink() or child.name.startswith("."):
            continue
        parsed = _read_manifest(child, data_dir)
        if parsed is None:
            continue
        manifest, database_path = parsed
        try:
            if database_path.stat().st_size != manifest.file_size:
                continue
            if _sha256(database_path) != manifest.sha256:
                continue
            _verify_snapshot_db(database_path, expected_database_id=expected_database_id)
        except (OSError, SessionStoreCorruptError):
            continue
        verified.append((manifest.created_at, child))
    verified.sort(key=lambda item: item[0], reverse=True)
    return [path for _created_at, path in verified]


def snapshot_summaries(
    data_dir: Path, *, expected_database_id: str | None = None
) -> list[dict[str, Any]]:
    """Return verified snapshot metadata without exposing Session content."""
    summaries: list[dict[str, Any]] = []
    for snapshot_dir in list_snapshots(data_dir, expected_database_id=expected_database_id):
        parsed = _read_manifest(snapshot_dir, data_dir)
        if parsed is None:
            continue
        manifest, _database_path = parsed
        summaries.append(_snapshot_summary(manifest))
    return summaries


def _snapshot_summary(manifest: SnapshotManifest) -> dict[str, Any]:
    return {
        "snapshot_id": manifest.snapshot_id,
        "created_at": manifest.created_at,
        "reason": manifest.reason,
        "database_id": manifest.database_id,
        "schema_version": manifest.schema_version,
        "file_size": manifest.file_size,
        "sha256": manifest.sha256,
        "session_count": manifest.session_count,
        "message_count": manifest.message_count,
        "latest_history_revision": manifest.latest_history_revision,
        "latest_state_revision": manifest.latest_state_revision,
        "integrity": manifest.integrity,
        "complete": manifest.complete,
    }


def _snapshot_inventory_entry(
    data_dir: Path,
    snapshot_dir: Path,
    *,
    expected_database_id: str | None = None,
) -> _SnapshotInventoryEntry | None:
    try:
        parsed = _read_manifest(snapshot_dir, data_dir)
    except OSError:
        return None
    if parsed is None:
        return None
    manifest, database_path = parsed
    try:
        if database_path.stat().st_size != manifest.file_size:
            return None
    except OSError:
        return None
    if expected_database_id is not None and manifest.database_id != expected_database_id:
        return None
    return _SnapshotInventoryEntry(path=Path(snapshot_dir), manifest=manifest)


def read_snapshot_summary(
    data_dir: Path,
    snapshot_dir: Path,
    *,
    expected_database_id: str | None = None,
) -> dict[str, Any] | None:
    """Read one strict published manifest and matching file stat without deep verification."""

    entry = _snapshot_inventory_entry(
        data_dir,
        snapshot_dir,
        expected_database_id=expected_database_id,
    )
    return None if entry is None else _snapshot_summary(entry.manifest)


def snapshot_inventory(
    data_dir: Path, *, expected_database_id: str | None = None
) -> list[dict[str, Any]]:
    """Return strict published-manifest metadata without rereading multi-GB databases."""
    root = snapshot_root(data_dir)
    if not root.is_dir():
        return []
    candidates: list[tuple[str, dict[str, Any]]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir() or child.is_symlink() or child.name.startswith("."):
            continue
        entry = _snapshot_inventory_entry(
            data_dir,
            child,
            expected_database_id=expected_database_id,
        )
        if entry is None:
            continue
        candidates.append(
            (
                entry.manifest.created_at,
                _snapshot_summary(entry.manifest),
            )
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [summary for _created_at, summary in candidates]


def _prune_snapshots(root: Path, *, protected_snapshot: Path) -> None:
    """Apply retention only after a verified snapshot has been published."""

    try:
        children = list(root.iterdir())
    except OSError:
        return
    entries = [
        entry
        for child in children
        if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
        if (entry := _snapshot_inventory_entry(root.parent, child)) is not None
    ]
    protected = next((entry for entry in entries if entry.path == protected_snapshot), None)
    if protected is None:
        return
    others = sorted(
        (entry for entry in entries if entry.path != protected_snapshot),
        key=lambda entry: (entry.manifest.created_at, entry.manifest.snapshot_id),
        reverse=True,
    )
    if not others:
        return
    total = protected.manifest.file_size
    retained_count = 1
    remove: list[Path] = []
    for entry in others:
        if (
            retained_count >= SNAPSHOT_KEEP_COUNT
            or total + entry.manifest.file_size > SNAPSHOT_KEEP_BYTES
        ):
            remove.append(entry.path)
        else:
            retained_count += 1
            total += entry.manifest.file_size
    for snapshot in remove:
        with suppress(OSError):
            __import__("shutil").rmtree(snapshot, ignore_errors=True)


def _bundle_members(database_path: Path) -> list[Path]:
    return [
        path
        for path in (
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
            Path(f"{database_path}-journal"),
        )
        if path.exists()
    ]


def _quarantine_database(database_path: Path) -> QuarantineResult:
    """Move a complete bundle, or roll back every member on the first failure."""

    data_dir = database_path.parent
    members = _bundle_members(database_path)
    if not members:
        return QuarantineResult("no_bundle")
    try:
        root = data_dir / "session-quarantine"
        root.mkdir(parents=True, exist_ok=True)
        batch = root / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        batch.mkdir()
    except OSError as exc:
        return QuarantineResult("failed", reason=str(exc))
    moved: list[tuple[Path, Path]] = []
    try:
        for member in members:
            destination = batch / member.name
            os.replace(member, destination)
            moved.append((member, destination))
    except OSError as exc:
        for original, destination in reversed(moved):
            with suppress(OSError):
                os.replace(destination, original)
        with suppress(OSError):
            __import__("shutil").rmtree(batch, ignore_errors=True)
        return QuarantineResult("failed", reason=str(exc))
    return QuarantineResult("success", path=batch)


def quarantine_database(database_path: Path) -> QuarantineResult:
    """Quarantine a database bundle for explicit recovery operations."""

    return _quarantine_database(Path(database_path))


def _restore_snapshot_locked(
    data_dir: Path,
    database_path: Path,
    snapshot_dir: Path,
) -> bool:
    parsed = _read_manifest(snapshot_dir, data_dir)
    if parsed is None:
        return False
    manifest, database_source = parsed
    marker_id = None
    try:
        from core.sessions.format import read_session_store_marker

        marker = read_session_store_marker(data_dir)
        marker_id = None if marker is None else str(marker["database_id"])
    except Exception:
        return False
    if marker_id is not None and manifest.database_id != marker_id:
        return False
    try:
        _verify_snapshot_db(database_source, expected_database_id=manifest.database_id)
        if database_source.stat().st_size != manifest.file_size:
            return False
        if _sha256(database_source) != manifest.sha256:
            return False
    except (OSError, SessionStoreCorruptError):
        return False
    from core.sessions.sqlite_runtime import has_live_connection

    if has_live_connection(database_path):
        return False
    quarantine = _quarantine_database(database_path)
    if quarantine.had_bundle and not quarantine.succeeded:
        return False
    temporary = database_path.with_name(f".{database_path.name}.restore.{uuid.uuid4().hex}.tmp")
    try:
        __import__("shutil").copy2(database_source, temporary)
        _fsync_file(temporary)
        os.replace(temporary, database_path)
        _fsync_dir(database_path.parent)
        _verify_snapshot_db(database_path, expected_database_id=manifest.database_id)
        return True
    except (OSError, sqlite3.Error, SessionStoreCorruptError):
        with suppress(OSError):
            temporary.unlink()
        with suppress(OSError):
            database_path.unlink()
        return False


def restore_snapshot(
    data_dir: Path,
    database_path: Path,
    snapshot_dir: Path,
    *,
    _lock_held: bool = False,
) -> bool:
    """Restore one strict candidate while preserving the original bundle."""

    root = snapshot_root(data_dir)
    lock = None if _lock_held else _acquire_lock(root / SNAPSHOT_LOCK_NAME, timeout=10.0)
    if not _lock_held and lock is None:
        return False
    try:
        return _restore_snapshot_locked(data_dir, database_path, snapshot_dir)
    finally:
        if lock is not None:
            _release_lock(lock)


RECOVERY_INCIDENT_FILE = "session-recovery.json"


def _incident_path(data_dir: Path) -> Path:
    return Path(data_dir) / RECOVERY_INCIDENT_FILE


def write_recovery_incident(
    data_dir: Path,
    *,
    cause: str,
    quarantine_path: Path | str | None,
    restored_snapshot_id: str,
    restored_snapshot_time: str,
    failure_detected_at: str | None = None,
    verification: str = "ok",
    incident_id: str | None = None,
    recovered_at: str | None = None,
) -> None:
    """Publish the durable recovery incident or raise before reporting success."""

    detected_at = failure_detected_at or _utc_now()
    resolved_recovered_at = None if verification == "pending" else (recovered_at or _utc_now())
    payload = {
        "incident_id": incident_id or uuid.uuid4().hex,
        "cause": cause,
        "quarantine": str(quarantine_path) if quarantine_path else None,
        "restored_snapshot_id": restored_snapshot_id,
        "restored_snapshot_time": restored_snapshot_time,
        "recovered_at": resolved_recovered_at,
        "verification": verification,
        "possible_loss_interval": {
            "start": restored_snapshot_time,
            "end": detected_at,
        },
        "acknowledged": False,
    }
    path = _incident_path(data_dir)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _fsync_file(temporary)
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink()
        raise SessionStoreUnavailableError(
            f"recovery incident could not be durably published: {path}"
        ) from exc


def _pending_incident_for_snapshot(data_dir: Path, snapshot_id: str) -> dict[str, Any] | None:
    incident = read_recovery_incident(data_dir)
    if (
        incident
        and incident.get("verification") == "pending"
        and incident.get("restored_snapshot_id") == snapshot_id
        and isinstance(incident.get("incident_id"), str)
    ):
        return incident
    return None


def _restore_snapshot_with_incident_locked(
    data_dir: Path,
    database_path: Path,
    snapshot_dir: Path,
    *,
    cause: str,
    failure_detected_at: str,
) -> bool:
    parsed = _read_manifest(snapshot_dir, data_dir)
    if parsed is None:
        return False
    manifest, _database_source = parsed
    pending = _pending_incident_for_snapshot(data_dir, manifest.snapshot_id)
    incident_id = str(pending["incident_id"]) if pending else uuid.uuid4().hex
    write_recovery_incident(
        data_dir,
        cause=cause,
        quarantine_path=None,
        restored_snapshot_id=manifest.snapshot_id,
        restored_snapshot_time=manifest.created_at,
        failure_detected_at=failure_detected_at,
        verification="pending",
        incident_id=incident_id,
        recovered_at=None,
    )
    quarantine_root = data_dir / "session-quarantine"
    before = set(quarantine_root.iterdir()) if quarantine_root.exists() else set()
    if not _restore_snapshot_locked(data_dir, database_path, snapshot_dir):
        return False
    after = set(quarantine_root.iterdir()) if quarantine_root.exists() else set()
    created = sorted(after - before, key=lambda path: path.name)
    quarantine_path = created[-1] if created else None
    try:
        write_recovery_incident(
            data_dir,
            cause=cause,
            quarantine_path=quarantine_path,
            restored_snapshot_id=manifest.snapshot_id,
            restored_snapshot_time=manifest.created_at,
            failure_detected_at=failure_detected_at,
            verification="ok",
            incident_id=incident_id,
            recovered_at=_utc_now(),
        )
    except SessionStoreUnavailableError:
        return False
    return True


def restore_snapshot_with_incident(
    data_dir: Path,
    database_path: Path,
    snapshot_dir: Path,
    *,
    cause: str,
) -> bool:
    """Run the shared restore state machine with a durable pre-mutation incident."""

    root = snapshot_root(data_dir)
    lock = _acquire_lock(root / SNAPSHOT_LOCK_NAME, timeout=10.0)
    if lock is None:
        return False
    try:
        return _restore_snapshot_with_incident_locked(
            data_dir,
            database_path,
            snapshot_dir,
            cause=cause,
            failure_detected_at=_utc_now(),
        )
    finally:
        _release_lock(lock)


def read_recovery_incident(data_dir: Path) -> dict[str, Any] | None:
    path = _incident_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def acknowledge_recovery_incident(data_dir: Path, incident_id: str | None = None) -> bool:
    """Acknowledge only the incident currently observed by the caller."""

    lock_path = snapshot_root(data_dir) / SNAPSHOT_LOCK_NAME
    try:
        lock = _acquire_lock(lock_path, timeout=SNAPSHOT_LOCK_TIMEOUT_SECONDS)
    except OSError as exc:
        raise SessionStoreUnavailableError(
            "recovery incident acknowledgement lock is unavailable"
        ) from exc
    if lock is None:
        raise SessionStoreUnavailableError("recovery incident acknowledgement lock is busy")
    path = _incident_path(data_dir)
    try:
        try:
            serialized = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except UnicodeError as exc:
            raise SessionStoreCorruptError("recovery incident is not valid UTF-8") from exc
        except OSError as exc:
            raise SessionStoreUnavailableError("recovery incident could not be read") from exc
        try:
            payload = json.loads(serialized)
        except json.JSONDecodeError as exc:
            raise SessionStoreCorruptError("recovery incident is not valid JSON") from exc
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("incident_id"), str)
            or not payload["incident_id"]
            or not isinstance(payload.get("acknowledged"), bool)
        ):
            raise SessionStoreCorruptError("recovery incident has an invalid shape")
        current_id = payload.get("incident_id")
        if incident_id is not None and current_id != incident_id:
            raise SessionRecoveryConflictError("recovery incident has changed; refresh status")
        if payload.get("acknowledged") is True:
            return True
        payload["acknowledged"] = True
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            _fsync_file(temporary)
            os.replace(temporary, path)
            _fsync_dir(path.parent)
        except OSError as exc:
            raise SessionStoreUnavailableError(
                "recovery incident acknowledgement could not be durably published"
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink()
        return True
    finally:
        _release_lock(lock)


def _canonical_probe(data_dir: Path, database_path: Path) -> _CanonicalProbe:
    detected_at = _utc_now()
    try:
        from core.sessions.format import read_session_store_marker

        marker = read_session_store_marker(data_dir)
        expected_id = None if marker is None else str(marker["database_id"])
    except Exception:
        expected_id = None
    if not database_path.exists():
        return _CanonicalProbe(False, True, "missing canonical Session database", detected_at)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(readonly_sqlite_uri(database_path), uri=True)
        app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            return _CanonicalProbe(False, False, "newer Session schema is unsupported", detected_at)
        if app_id != APPLICATION_ID or version != SCHEMA_VERSION:
            return _CanonicalProbe(False, True, "Session schema identity is invalid", detected_at)
        row = connection.execute(
            "SELECT value FROM store_meta WHERE key = ?", (DATABASE_ID_META_KEY,)
        ).fetchone()
        internal_id = None if row is None else row[0]
        if not _valid_database_id(internal_id) or (
            expected_id is not None and internal_id != expected_id
        ):
            return _CanonicalProbe(False, True, "Session database identity mismatch", detected_at)
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            return _CanonicalProbe(False, True, "Session database integrity failure", detected_at)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            return _CanonicalProbe(False, True, "Session foreign-key failure", detected_at)
        return _CanonicalProbe(True, False, "", detected_at)
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if any(
            marker in message
            for marker in (
                "busy",
                "locked",
                "readonly",
                "read-only",
                "disk full",
                "disk i/o",
                "permission",
            )
        ):
            return _CanonicalProbe(False, False, "operational Session-store failure", detected_at)
        return _CanonicalProbe(False, True, "malformed canonical Session database", detected_at)
    except sqlite3.DatabaseError:
        return _CanonicalProbe(False, True, "malformed canonical Session database", detected_at)
    finally:
        if connection is not None:
            with suppress(BaseException):
                connection.close()


def auto_restore_if_needed(data_dir: Path, database_path: Path) -> bool:
    """Recover under one lock after a locked, authoritative canonical re-probe."""

    root = snapshot_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = _acquire_lock(root / SNAPSHOT_LOCK_NAME, timeout=10.0)
    if lock is None:
        return False
    try:
        probe = _canonical_probe(data_dir, database_path)
        if probe.usable:
            pending = read_recovery_incident(data_dir)
            if pending and pending.get("verification") == "pending":
                with suppress(KeyError, SessionStoreUnavailableError):
                    write_recovery_incident(
                        data_dir,
                        cause=str(pending.get("cause") or "Session recovery"),
                        quarantine_path=pending.get("quarantine"),
                        restored_snapshot_id=str(pending.get("restored_snapshot_id") or "unknown"),
                        restored_snapshot_time=str(
                            pending.get("restored_snapshot_time") or probe.failure_detected_at
                        ),
                        failure_detected_at=str(
                            pending.get("possible_loss_interval", {}).get(
                                "end", probe.failure_detected_at
                            )
                        ),
                        verification="ok",
                        incident_id=str(pending["incident_id"]),
                        recovered_at=_utc_now(),
                    )
            return False
        if not probe.recoverable:
            return False
        expected_id = None
        try:
            from core.sessions.format import read_session_store_marker

            marker = read_session_store_marker(data_dir)
            expected_id = None if marker is None else str(marker["database_id"])
        except Exception:
            return False
        for snapshot_dir in list_snapshots(data_dir, expected_database_id=expected_id):
            try:
                restored = _restore_snapshot_with_incident_locked(
                    data_dir,
                    database_path,
                    snapshot_dir,
                    cause=probe.cause,
                    failure_detected_at=probe.failure_detected_at,
                )
            except SessionStoreUnavailableError:
                return False
            if restored:
                return True
        return False
    finally:
        _release_lock(lock)
