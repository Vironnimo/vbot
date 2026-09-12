"""Canonical database recovery, quarantine and durable incident lifecycle."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.sessions import snapshots
from core.sessions.errors import (
    QuarantineResult,
    SessionRecoveryConflictError,
    SessionStoreCorruptError,
    SessionStoreUnavailableError,
)
from core.sessions.schema import APPLICATION_ID, DATABASE_ID_META_KEY, SCHEMA_VERSION
from core.sessions.sqlite_runtime import readonly_sqlite_uri


@dataclass(frozen=True)
class _CanonicalProbe:
    usable: bool
    recoverable: bool
    cause: str
    failure_detected_at: str


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
    parsed = snapshots._read_manifest(snapshot_dir, data_dir)
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
        snapshots._verify_snapshot_db(database_source, expected_database_id=manifest.database_id)
        if database_source.stat().st_size != manifest.file_size:
            return False
        if snapshots._sha256(database_source) != manifest.sha256:
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
        snapshots._fsync_file(temporary)
        os.replace(temporary, database_path)
        snapshots._fsync_dir(database_path.parent)
        snapshots._verify_snapshot_db(database_path, expected_database_id=manifest.database_id)
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

    root = snapshots.snapshot_root(data_dir)
    lock = (
        None
        if _lock_held
        else snapshots._acquire_lock(root / snapshots.SNAPSHOT_LOCK_NAME, timeout=10.0)
    )
    if not _lock_held and lock is None:
        return False
    try:
        return _restore_snapshot_locked(data_dir, database_path, snapshot_dir)
    finally:
        if lock is not None:
            snapshots._release_lock(lock)


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

    detected_at = failure_detected_at or snapshots._utc_now()
    resolved_recovered_at = (
        None if verification == "pending" else (recovered_at or snapshots._utc_now())
    )
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
        snapshots._fsync_file(temporary)
        os.replace(temporary, path)
        snapshots._fsync_dir(path.parent)
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
    parsed = snapshots._read_manifest(snapshot_dir, data_dir)
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
            recovered_at=snapshots._utc_now(),
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

    root = snapshots.snapshot_root(data_dir)
    lock = snapshots._acquire_lock(root / snapshots.SNAPSHOT_LOCK_NAME, timeout=10.0)
    if lock is None:
        return False
    try:
        return _restore_snapshot_with_incident_locked(
            data_dir,
            database_path,
            snapshot_dir,
            cause=cause,
            failure_detected_at=snapshots._utc_now(),
        )
    finally:
        snapshots._release_lock(lock)


def read_recovery_incident(data_dir: Path) -> dict[str, Any] | None:
    path = _incident_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def acknowledge_recovery_incident(data_dir: Path, incident_id: str | None = None) -> bool:
    """Acknowledge only the incident currently observed by the caller."""

    lock_path = snapshots.snapshot_root(data_dir) / snapshots.SNAPSHOT_LOCK_NAME
    try:
        lock = snapshots._acquire_lock(lock_path, timeout=snapshots.SNAPSHOT_LOCK_TIMEOUT_SECONDS)
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
            snapshots._fsync_file(temporary)
            os.replace(temporary, path)
            snapshots._fsync_dir(path.parent)
        except OSError as exc:
            raise SessionStoreUnavailableError(
                "recovery incident acknowledgement could not be durably published"
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink()
        return True
    finally:
        snapshots._release_lock(lock)


def _canonical_probe(data_dir: Path, database_path: Path) -> _CanonicalProbe:
    detected_at = snapshots._utc_now()
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
        if not snapshots._valid_database_id(internal_id) or (
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

    root = snapshots.snapshot_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = snapshots._acquire_lock(root / snapshots.SNAPSHOT_LOCK_NAME, timeout=10.0)
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
                        recovered_at=snapshots._utc_now(),
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
        for snapshot_dir in snapshots.list_snapshots(data_dir, expected_database_id=expected_id):
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
        snapshots._release_lock(lock)
