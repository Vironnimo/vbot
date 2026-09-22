"""Canonical database recovery, quarantine and durable incident lifecycle."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing, suppress
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
from core.sessions.sqlite_runtime import (
    classify_unavailable,
    classify_write_error,
    readonly_sqlite_uri,
)


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


def _new_quarantine_path(data_dir: Path) -> Path:
    return (
        data_dir
        / "session-quarantine"
        / (f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}")
    )


def _quarantine_database(
    database_path: Path, *, destination: Path | None = None
) -> QuarantineResult:
    """Move a complete bundle, or roll back every member on the first failure."""

    data_dir = database_path.parent
    members = _bundle_members(database_path)
    if not members:
        return QuarantineResult("no_bundle")
    try:
        root = data_dir / "session-quarantine"
        root.mkdir(parents=True, exist_ok=True)
        batch = destination or _new_quarantine_path(data_dir)
        batch.mkdir()
    except OSError as exc:
        return QuarantineResult("failed", reason=str(exc))
    moved: list[tuple[Path, Path]] = []
    try:
        for member in members:
            destination = batch / member.name
            os.replace(member, destination)
            moved.append((member, destination))
        snapshots._fsync_dir(batch)
        snapshots._fsync_dir(root)
        snapshots._fsync_dir(data_dir)
    except OSError as exc:
        rollback_errors: list[str] = []
        for original, destination in reversed(moved):
            try:
                os.replace(destination, original)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        # Never recursively delete evidence: a failed rollback can leave the
        # only copy of a bundle member here. Only remove an empty directory.
        with suppress(OSError):
            batch.rmdir()
        reason = str(exc)
        if rollback_errors:
            reason += f"; rollback failed; retained bundle at {batch}: " + "; ".join(
                rollback_errors
            )
        return QuarantineResult("failed", path=batch if rollback_errors else None, reason=reason)
    return QuarantineResult("success", path=batch)


def quarantine_database(database_path: Path) -> QuarantineResult:
    """Quarantine a database bundle for explicit recovery operations."""

    return _quarantine_database(Path(database_path))


def _snapshot_schema_compatible(database_source: Path) -> bool:
    """Reject incompatible application shapes before any recovery mutation."""
    from core.sessions.schema import reconcile_schema

    try:
        with closing(sqlite3.connect(readonly_sqlite_uri(database_source), uri=True)) as source:
            reconcile_schema(source, dry_run=True)
        return True
    except sqlite3.Error as exc:
        if classify_write_error(exc) == "unavailable" or classify_unavailable(exc):
            raise SessionStoreUnavailableError("snapshot schema could not be read") from exc
        return False
    except SessionStoreCorruptError:
        return False
    except OSError as exc:
        raise SessionStoreUnavailableError("snapshot schema could not be read") from exc


def _prepare_snapshot(
    data_dir: Path, database_path: Path, snapshot_dir: Path
) -> tuple[snapshots.SnapshotManifest, Path] | None:
    """Reject invalid candidates before changing canonical data or incident evidence."""
    from core.sessions.format import read_session_store_marker, validate_session_store_paths
    from core.sessions.sqlite_runtime import has_live_connection

    validate_session_store_paths(data_dir, database_path)
    parsed = snapshots._read_manifest(snapshot_dir, data_dir)
    if parsed is None:
        return None
    manifest, database_source = parsed
    marker = read_session_store_marker(data_dir)
    if marker is None or manifest.database_id != marker["database_id"]:
        return None
    if has_live_connection(database_path) or not _snapshot_schema_compatible(database_source):
        return None
    try:
        snapshots._verify_snapshot_manifest(database_source, manifest)
    except SessionStoreCorruptError:
        return None
    return parsed


def _install_snapshot(
    database_path: Path,
    manifest: snapshots.SnapshotManifest,
    database_source: Path,
    *,
    before_replace: Callable[[Path | None], None] | None = None,
) -> Path | None:
    """Stage verified bytes before quarantine; operational failures never select older data."""
    temporary = database_path.with_name(f".{database_path.name}.restore.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(database_source, temporary)
        snapshots._verify_snapshot_manifest(temporary, manifest)
        snapshots._fsync_file(temporary)
        quarantine_path = (
            _new_quarantine_path(database_path.parent) if _bundle_members(database_path) else None
        )
        if before_replace is not None:
            before_replace(quarantine_path)
        quarantine = _quarantine_database(database_path, destination=quarantine_path)
        if quarantine.had_bundle and not quarantine.succeeded:
            raise SessionStoreUnavailableError(quarantine.reason or "Session quarantine failed")
        os.replace(temporary, database_path)
        snapshots._fsync_dir(database_path.parent)
        snapshots._verify_snapshot_db(database_path, expected_database_id=manifest.database_id)
        return quarantine.path
    except (OSError, sqlite3.Error, SessionStoreCorruptError) as exc:
        # The published database or quarantined original can be the only good copy.
        # Keep both on post-publication failures; the next open verifies them again.
        raise SessionStoreUnavailableError("Session snapshot could not be installed") from exc
    finally:
        with suppress(OSError):
            temporary.unlink()


def _restore_snapshot_locked(
    data_dir: Path,
    database_path: Path,
    snapshot_dir: Path,
) -> bool:
    prepared = _prepare_snapshot(data_dir, database_path, snapshot_dir)
    if prepared is None:
        return False
    manifest, database_source = prepared
    _install_snapshot(database_path, manifest, database_source)
    return True


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
    prepared = _prepare_snapshot(data_dir, database_path, snapshot_dir)
    if prepared is None:
        return False
    manifest, database_source = prepared
    pending = _pending_incident_for_snapshot(data_dir, manifest.snapshot_id)
    incident_id = str(pending["incident_id"]) if pending else uuid.uuid4().hex
    retained_quarantine = pending["quarantine"] if pending else None
    if retained_quarantine and not _bundle_members(Path(retained_quarantine) / database_path.name):
        # A fully rolled-back attempt only reserved this path; it holds no evidence.
        retained_quarantine = None
    if pending is not None:
        failure_detected_at = pending["possible_loss_interval"]["end"]
        cause = pending["cause"]

    def publish_pending(quarantine_path: Path | None) -> None:
        write_recovery_incident(
            data_dir,
            cause=cause,
            quarantine_path=retained_quarantine or quarantine_path,
            restored_snapshot_id=manifest.snapshot_id,
            restored_snapshot_time=manifest.created_at,
            failure_detected_at=failure_detected_at,
            verification="pending",
            incident_id=incident_id,
        )

    quarantine_path = _install_snapshot(
        database_path, manifest, database_source, before_replace=publish_pending
    )
    # Publication failure is operational, not evidence that this snapshot is
    # unusable. Propagate it so auto-recovery stops instead of restoring an
    # older candidate over the already verified database.
    write_recovery_incident(
        data_dir,
        cause=cause,
        quarantine_path=retained_quarantine or quarantine_path,
        restored_snapshot_id=manifest.snapshot_id,
        restored_snapshot_time=manifest.created_at,
        failure_detected_at=failure_detected_at,
        verification="ok",
        incident_id=incident_id,
        recovered_at=snapshots._utc_now(),
    )
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
        serialized = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
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
        or not {
            "incident_id",
            "cause",
            "restored_snapshot_id",
            "restored_snapshot_time",
            "verification",
            "recovered_at",
            "quarantine",
            "possible_loss_interval",
            "acknowledged",
        }.issubset(payload)
        or any(
            not isinstance(payload.get(key), str) or not payload[key]
            for key in ("incident_id", "cause", "restored_snapshot_id", "restored_snapshot_time")
        )
        or not isinstance(payload.get("acknowledged"), bool)
        or payload.get("verification") not in ("pending", "ok")
        or not isinstance(payload.get("possible_loss_interval"), dict)
        or any(
            not isinstance(payload["possible_loss_interval"].get(key), str)
            or not payload["possible_loss_interval"][key]
            for key in ("start", "end")
        )
        or (payload.get("quarantine") is not None and not isinstance(payload["quarantine"], str))
        or (
            payload["verification"] == "ok"
            and (not isinstance(payload.get("recovered_at"), str) or not payload["recovered_at"])
        )
        or (payload["verification"] == "pending" and payload.get("recovered_at") is not None)
    ):
        raise SessionStoreCorruptError("recovery incident has an invalid shape")
    for value in (
        payload["restored_snapshot_time"],
        payload["possible_loss_interval"]["start"],
        payload["possible_loss_interval"]["end"],
        *([payload["recovered_at"]] if payload["verification"] == "ok" else []),
    ):
        try:
            if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("timestamp is not timezone-aware")
        except ValueError as exc:
            raise SessionStoreCorruptError("recovery incident has an invalid timestamp") from exc
    return payload


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
        payload = read_recovery_incident(data_dir)
        if payload is None:
            return False
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
        if not _snapshot_schema_compatible(database_path):
            return _CanonicalProbe(False, True, "Session table schema mismatch", detected_at)
        return _CanonicalProbe(True, False, "", detected_at)
    except SessionStoreUnavailableError:
        return _CanonicalProbe(False, False, "operational Session-store failure", detected_at)
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if classify_write_error(exc) == "unavailable" or any(
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
            if pending and pending["verification"] == "pending":
                parsed = snapshots._read_manifest(
                    snapshots.snapshot_root(data_dir) / pending["restored_snapshot_id"], data_dir
                )
                if parsed is None:
                    return False
                manifest, _source = parsed
                try:
                    snapshots._verify_snapshot_manifest(database_path, manifest)
                except SessionStoreCorruptError:
                    # Interruption before quarantine left the original usable. It
                    # is not evidence that the requested snapshot was restored.
                    return False
                write_recovery_incident(
                    data_dir,
                    cause=pending["cause"],
                    quarantine_path=pending["quarantine"],
                    restored_snapshot_id=pending["restored_snapshot_id"],
                    restored_snapshot_time=pending["restored_snapshot_time"],
                    failure_detected_at=pending["possible_loss_interval"]["end"],
                    verification="ok",
                    incident_id=pending["incident_id"],
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
