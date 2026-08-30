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


def _acquire_lock(lock_path: Path, timeout: float = 10.0) -> Path | None:
    """Try to acquire a cross-process lock file. Returns lock file path on success."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock_path
        except FileExistsError:
            # Check if holder died (stale lock older than 60s).
            try:
                mtime = lock_path.stat().st_mtime
                if time.monotonic() - mtime > 60:
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
    with suppress(OSError):
        lock_path.unlink()


def _verify_snapshot_db(path: Path, expected_database_id: str | None = None) -> tuple[int, int]:
    """Run application/schema/identity/structural/integrity/FK/count checks.

    Returns (session_count, message_count) on success, raises SessionStoreCorruptError on failure.
    """
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            # Application id and schema version.
            app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
            if app_id != APPLICATION_ID:
                raise SessionStoreCorruptError(f"snapshot application_id mismatch: {app_id}")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise SessionStoreCorruptError(
                    f"snapshot schema version {version} != {SCHEMA_VERSION}"
                )
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
            manifest = {
                "manifest_version": MANIFEST_VERSION,
                "snapshot_id": snapshot_id,
                "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "database_id": database_id or "",
                "schema_version": SCHEMA_VERSION,
                "application_id": APPLICATION_ID,
                "sha256": sha,
                "session_count": session_count,
                "message_count": message_count,
                "database_file": SNAPSHOT_DATABASE_NAME,
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
    snapshots = list_snapshots(root)
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
        except Exception:
            return False
        # Ensure no live connection (check via sqlite_runtime tracking).
        from core.sessions.sqlite_runtime import has_live_connection

        if has_live_connection(database_path):
            return False
        # Quarantine current DB as bundle.
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
            # Verify restored DB.
            try:
                _verify_snapshot_db(database_path)
            except Exception:
                with suppress(Exception):
                    tmp_restore.unlink(missing_ok=True)
                return False
            return True
        finally:
            with suppress(Exception):
                tmp_restore.unlink(missing_ok=True)
    finally:
        _release_lock(lock)


def auto_restore_if_needed(data_dir: Path, database_path: Path) -> bool:
    """If canonical DB is missing/corrupt and a verified snapshot exists, restore newest.

    Returns True if a restore was performed and the DB is now usable.
    """
    # Check if DB is already usable.
    try:
        with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as conn:
            conn.execute("SELECT 1 FROM store_meta LIMIT 1")
            # Quick integrity check.
            if str(conn.execute("PRAGMA quick_check").fetchone()[0]) == "ok":
                return False
    except Exception:
        pass
    snapshots = list_snapshots(data_dir)
    return any(restore_snapshot(data_dir, database_path, snap) for snap in snapshots)  # noqa: SIM110
