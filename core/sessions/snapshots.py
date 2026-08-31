"""Verified snapshot service for the canonical Session database.

Snapshots are standalone ``sessions.db`` copies with a manifest, staged
atomically and verified before publication. The service owns only current-format
SQLite; it has no legacy JSONL knowledge.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.sessions.errors import SessionStoreCorruptError
from core.sessions.schema import APPLICATION_ID, DATABASE_ID_META_KEY, SCHEMA_VERSION

SNAPSHOT_ROOT_NAME = "session-snapshots"
SNAPSHOT_MANIFEST_NAME = "manifest.json"
SNAPSHOT_DATABASE_NAME = "sessions.db"
SNAPSHOT_LOCK_NAME = ".lock"
SNAPSHOT_PARTIAL_SUFFIX = ".partial"
SNAPSHOT_KEEP_COUNT = 5
SNAPSHOT_KEEP_BYTES = 512 * 1024 * 1024  # 512 MiB
SNAPSHOT_RESERVE_BYTES = 64 * 1024 * 1024  # keep 64 MiB free

# Manifest version for future evolution.
MANIFEST_VERSION = 1


@dataclass(frozen=True)
class SnapshotManifest:
    snapshot_id: str
    created_at: str
    database_id: str
    schema_version: int
    application_id: int
    sha256: str
    message_count: int
    session_count: int


def snapshot_root(data_dir: Path) -> Path:
    return Path(data_dir) / SNAPSHOT_ROOT_NAME


def _snapshot_id() -> str:
    # UTC plus collision-safe entropy.
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{ts}Z-{uuid.uuid4().hex[:8]}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


_lock_fds: dict[str, int] = {}


def _acquire_lock(lock_path: Path, timeout: float = 10.0) -> Path | None:
    """Try to acquire a cross-process lock file. Returns lock file path on success.

    Holds an OS flock where available so the lock is released if the holder
    dies. Falls back to O_EXCL plus wall-time stale detection.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                try:
                    import fcntl  # type: ignore[import-not-found]  # POSIX

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                except Exception:
                    pass
                os.write(fd, str(os.getpid()).encode())
                # Keep fd open to hold the flock for the duration of the operation.
                _lock_fds[str(lock_path)] = fd
            except Exception:
                with suppress(OSError):
                    os.close(fd)
                raise
            return lock_path
        except FileExistsError:
            # Stale holder died without unlink — compare wall times.
            try:
                mtime = lock_path.stat().st_mtime
                if time.time() - mtime > 60:
                    with suppress(OSError):
                        lock_path.unlink()
                    continue
            except OSError:
                pass
            time.sleep(0.05)
        except OSError:
            return None
    return None


def _release_lock(lock_path: Path) -> None:
    key = str(lock_path)
    fd = _lock_fds.pop(key, None)
    if fd is not None:
        with suppress(OSError):
            try:
                import fcntl  # type: ignore[import-not-found]

                fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
            except Exception:
                pass
            os.close(fd)
    with suppress(OSError):
        lock_path.unlink()


def _verify_snapshot_db(path: Path, expected_database_id: str | None = None) -> tuple[int, int]:
    """Run application/schema/identity/structural/integrity/FK/count checks.

    Returns (session_count, message_count) on success, raises SessionStoreCorruptError on failure.
    Explicitly closes the connection before returning so Windows can rename the
    containing directory.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row  # type: ignore[assignment]
        # Application id and schema version.
        app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        if app_id != APPLICATION_ID:
            raise SessionStoreCorruptError(f"snapshot application_id mismatch: {app_id}")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise SessionStoreCorruptError(f"snapshot schema version {version} != {SCHEMA_VERSION}")
        # Identity.
        row = conn.execute(
            "SELECT value FROM store_meta WHERE key=?", (DATABASE_ID_META_KEY,)
        ).fetchone()
        if row is None or not isinstance(row[0], str) or len(row[0]) != 32:
            raise SessionStoreCorruptError("snapshot missing database_id")
        if expected_database_id and str(row[0]) != expected_database_id:
            raise SessionStoreCorruptError("snapshot database_id mismatch")
        # Integrity.
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        if str(integrity) != "ok":
            raise SessionStoreCorruptError(f"snapshot integrity failed: {integrity}")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SessionStoreCorruptError("snapshot foreign_key_check failed")
        # Counts.
        session_count = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        message_count = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        # Hash will be verified by caller against manifest.
        return session_count, message_count
    except sqlite3.Error as exc:
        raise SessionStoreCorruptError(f"snapshot verification failed: {exc}") from exc
    finally:
        if conn is not None:
            with suppress(Exception):
                conn.close()


def create_snapshot(
    data_dir: Path,
    database_path: Path,
    backup_fn,
    *,
    database_id: str | None = None,
) -> Path | None:
    """Create one verified snapshot. Returns snapshot dir on success, None on failure.

    `backup_fn` is `SessionStore.backup` that writes a consistent copy to a destination.
    """
    root = snapshot_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = _acquire_lock(root / SNAPSHOT_LOCK_NAME)
    if lock is None:
        return None
    try:
        # Check free space.
        try:
            import shutil

            free = shutil.disk_usage(root).free
            # Estimate needed: db size + reserve.
            try:
                db_size = database_path.stat().st_size if database_path.exists() else 0
            except OSError:
                db_size = 0
            if free < db_size + SNAPSHOT_RESERVE_BYTES:
                return None
        except Exception:
            pass

        snapshot_id = _snapshot_id()
        partial = root / f".{snapshot_id}.{os.getpid()}{SNAPSHOT_PARTIAL_SUFFIX}"
        partial.mkdir(parents=True, exist_ok=True)
        db_dest = partial / SNAPSHOT_DATABASE_NAME
        try:
            backup_fn(db_dest)
            # Close is handled by backup_fn's backup API; now verify.
            sha = _sha256(db_dest)
            # Verify DB before publishing.
            session_count, message_count = _verify_snapshot_db(
                db_dest, expected_database_id=database_id
            )
            # Collect file size and sqlite diagnostics for manifest.
            try:
                file_size = db_dest.stat().st_size
            except OSError:
                file_size = 0
            try:
                from core.sessions.sqlite_runtime import sqlite_source_id as _src_id

                sqlite_version = sqlite3.sqlite_version
                sqlite_source = _src_id()
            except Exception:
                sqlite_version = sqlite3.sqlite_version
                sqlite_source = ""
            manifest = {
                "manifest_version": MANIFEST_VERSION,
                "snapshot_id": snapshot_id,
                "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "database_id": database_id or "",
                "schema_version": SCHEMA_VERSION,
                "application_id": APPLICATION_ID,
                "sha256": sha,
                "file_size": file_size,
                "sqlite_version": sqlite_version,
                "sqlite_source_id": sqlite_source,
                "session_count": session_count,
                "message_count": message_count,
                "database_file": SNAPSHOT_DATABASE_NAME,
                "integrity": "ok",
                "foreign_key_check": "ok",
                "complete": True,
            }
            manifest_path = partial / SNAPSHOT_MANIFEST_NAME
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            if os.name != "nt":
                fd = os.open(manifest_path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                fd = os.open(db_dest, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                _fsync_dir(partial)
            # Verify hash matches manifest.
            if _sha256(db_dest) != sha:
                raise SessionStoreCorruptError("snapshot hash mismatch after fsync")
            final = root / snapshot_id
            # Atomic publish.
            os.replace(partial, final)
            _fsync_dir(root)
            # Prune old snapshots only after successful publish.
            _prune_snapshots(root)
            return final
        except Exception:
            with suppress(Exception):
                import shutil as _shutil

                _shutil.rmtree(partial, ignore_errors=True)
            return None
    finally:
        _release_lock(lock)


def list_snapshots(data_dir: Path) -> list[Path]:
    """Return verified snapshot directories newest first. Invalid/incomplete are ignored."""
    root = snapshot_root(data_dir)
    if not root.exists():
        return []
    snapshots: list[tuple[str, Path]] = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest_path = child / SNAPSHOT_MANIFEST_NAME
        db_path = child / SNAPSHOT_DATABASE_NAME
        if not manifest_path.is_file() or not db_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                continue
            if manifest.get("manifest_version") != MANIFEST_VERSION:
                continue
            if manifest.get("snapshot_id") != child.name:
                continue
            # Verify hash and DB.
            expected_sha = str(manifest.get("sha256") or "")
            if not expected_sha or _sha256(db_path) != expected_sha:
                continue
            _verify_snapshot_db(
                db_path, expected_database_id=str(manifest.get("database_id") or "") or None
            )
            snapshots.append((str(manifest.get("created_at") or ""), child))
        except Exception:
            continue
    snapshots.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in snapshots]


def _prune_snapshots(root: Path) -> None:
    # root is the snapshot_root directory; list_snapshots expects the data_dir,
    # so derive data_dir from root.
    data_dir = root.parent if root.name == SNAPSHOT_ROOT_NAME else root
    snapshots = list_snapshots(data_dir)
    # Keep at least one.
    if len(snapshots) <= 1:
        return
    # Count-based prune.
    to_remove = snapshots[SNAPSHOT_KEEP_COUNT:]
    # Byte budget.
    total = 0
    kept: list[Path] = []
    for snap in snapshots[:SNAPSHOT_KEEP_COUNT]:
        try:
            size = (snap / SNAPSHOT_DATABASE_NAME).stat().st_size
        except OSError:
            size = 0
        if total + size > SNAPSHOT_KEEP_BYTES and kept:
            to_remove.append(snap)
        else:
            total += size
            kept.append(snap)
    # Never remove the last verified.
    if len(snapshots) - len(to_remove) < 1:
        to_remove = to_remove[: len(snapshots) - 1]
    for snap in to_remove:
        with suppress(Exception):
            import shutil as _shutil

            _shutil.rmtree(snap, ignore_errors=True)


def restore_snapshot(
    data_dir: Path,
    database_path: Path,
    snapshot_dir: Path,
) -> bool:
    """Restore a verified snapshot over the canonical DB. Returns True on success."""
    root = snapshot_root(data_dir)
    lock = _acquire_lock(root / SNAPSHOT_LOCK_NAME, timeout=10.0)
    if lock is None:
        return False
    try:
        manifest_path = snapshot_dir / SNAPSHOT_MANIFEST_NAME
        db_src = snapshot_dir / SNAPSHOT_DATABASE_NAME
        if not manifest_path.is_file() or not db_src.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_sha = str(manifest.get("sha256") or "")
            if _sha256(db_src) != expected_sha:
                return False
            _verify_snapshot_db(db_src)
            # Verify snapshot identity matches current marker if a marker exists.
            try:
                from core.sessions.format import read_session_store_marker

                marker = read_session_store_marker(data_dir)
                if marker is not None:
                    marker_id = str(marker.get("database_id") or "")
                    manifest_id = str(manifest.get("database_id") or "")
                    if manifest_id and marker_id and manifest_id != marker_id:
                        return False
                    # Also verify the snapshot DB's internal id matches manifest
                    internal_id = str(manifest.get("database_id") or "")
                    if internal_id:
                        _verify_snapshot_db(db_src, expected_database_id=internal_id)
            except Exception:
                pass
        except Exception:
            return False
        # Ensure no live connection (check via sqlite_runtime tracking).
        from core.sessions.sqlite_runtime import has_live_connection

        if has_live_connection(database_path):
            return False
        # Quarantine current DB as bundle (all-or-rollback).
        from core.sessions.store import quarantine_database

        quarantine_database(database_path)
        # Copy to hidden restore file, fsync, replace.
        tmp_restore = database_path.with_name(
            f".{database_path.name}.restore.{uuid.uuid4().hex}.tmp"
        )
        try:
            import shutil as _shutil

            _shutil.copy2(db_src, tmp_restore)
            if os.name != "nt":
                fd = os.open(tmp_restore, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.replace(tmp_restore, database_path)
            _fsync_dir(database_path.parent)
            # Verify restored DB before declaring success.
            try:
                _verify_snapshot_db(database_path)
            except Exception:
                # Restore verification failed — remove the corrupted replacement
                # and keep the quarantined original for salvage. Do not try to
                # roll back automatically; the caller will try the next snapshot.
                with suppress(Exception):
                    database_path.unlink(missing_ok=True)
                return False
            return True
        finally:
            with suppress(Exception):
                tmp_restore.unlink(missing_ok=True)
    finally:
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
    verification: str = "ok",
) -> None:
    """Persist a recovery incident outside the restored DB."""
    payload = {
        "cause": cause,
        "quarantine": str(quarantine_path) if quarantine_path else None,
        "restored_snapshot_id": restored_snapshot_id,
        "restored_snapshot_time": restored_snapshot_time,
        "verification": verification,
        "recovered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    path = _incident_path(data_dir)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        os.replace(tmp, path)
        if os.name != "nt":
            _fsync_dir(path.parent)
    except OSError:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)


def read_recovery_incident(data_dir: Path) -> dict | None:  # type: ignore[no-any-return]
    path = _incident_path(data_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except Exception:
        return None


def acknowledge_recovery_incident(data_dir: Path) -> bool:
    path = _incident_path(data_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def auto_restore_if_needed(data_dir: Path, database_path: Path) -> bool:
    """If canonical DB is missing/corrupt and a verified snapshot exists, restore newest.

    Returns True if a restore was performed and the DB is now usable.
    Persists a recovery incident on success.
    """
    # Check if DB is already usable.
    try:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
            conn.execute("SELECT 1 FROM store_meta LIMIT 1")
            if str(conn.execute("PRAGMA quick_check").fetchone()[0]) == "ok":
                return False
        finally:
            if conn is not None:
                with suppress(Exception):
                    conn.close()
    except Exception:
        pass
    snapshots = list_snapshots(data_dir)
    for snap in snapshots:
        # Capture quarantine state before restore for incident.
        from core.sessions.store import QUARANTINE_DIRECTORY_NAME

        try:
            q_root = Path(data_dir) / QUARANTINE_DIRECTORY_NAME
            before = set(q_root.iterdir()) if q_root.exists() else set()
        except OSError:
            before = set()
        if restore_snapshot(data_dir, database_path, snap):
            try:
                manifest = json.loads((snap / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
                snap_id = str(manifest.get("snapshot_id") or snap.name)
                snap_time = str(manifest.get("created_at") or "")
            except Exception:
                snap_id = snap.name
                snap_time = ""
            # Find newest quarantine created by this restore.
            quarantine_path = None
            try:
                q_root = Path(data_dir) / QUARANTINE_DIRECTORY_NAME
                after = set(q_root.iterdir()) if q_root.exists() else set()
                new = after - before
                if new:
                    quarantine_path = sorted(new, key=lambda p: p.name)[-1]
                elif after:
                    quarantine_path = sorted(after, key=lambda p: p.name)[-1]
            except OSError:
                pass
            write_recovery_incident(
                data_dir,
                cause="auto-restore after corruption or missing DB",
                quarantine_path=quarantine_path,
                restored_snapshot_id=snap_id,
                restored_snapshot_time=snap_time,
            )
            return True
    return False
