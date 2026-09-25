"""Pre-update data snapshots that an application updater may restore automatically.

An automatic restore after a failed update is safe only when it discards
nothing but the failed candidate's own writes. This module proves the data side
of that; the caller proves that no server process ran on the data directory:

- ``create_update_snapshot`` runs offline, after the previous server stopped.
  It records a stamp of the data (the marker, every registered database file
  with its journal files, every JSON document) that must be identical before
  and after the capture, and binds the snapshot to the update by reason and by
  the recorded member and document hashes.
- ``data_changed_since`` compares the current data with that stamp, before the
  candidate starts and again after it failed.
- ``restore_update_snapshot`` refuses with ``UpdateRollbackRefusedError``, and
  changes nothing, unless the snapshot is exactly the one this update took,
  still verifies completely, and the data directory shows no foreign activity
  (an incomplete maintenance operation, a changed database identity). It then
  restores every database member and the JSON document set and retires
  databases registered after the snapshot.

A snapshot is never restored automatically outside that window: after a
successful activation the snapshot is stale.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from core.database._documents import live_documents
from core.database._files import sha256_file
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    UpdateRollbackRefusedError,
)
from core.database.marker import marker_path, read_maintenance, read_marker
from core.database.recovery import SnapshotRestore, restore_data_snapshot
from core.database.snapshots import (
    create_data_snapshot,
    read_manifest,
    read_snapshot_health,
    snapshot_inventory,
    snapshot_root,
)
from core.database.spec import DatabaseSpec, canonical_database_path, canonical_relative_path

UPDATE_ROLLBACK_CAUSE = "automatic update rollback: the new version failed its startup check"
_STAMPED_SUFFIXES = ("", "-wal", "-journal")


@dataclass(frozen=True)
class DataStamp:
    """What the data directory looked like at one instant, cheap to compare.

    ``files`` maps each registered database file and journal to its size and
    modification time (``None`` when absent or an empty journal); ``documents``
    maps each JSON document to its SHA-256.
    """

    marker: str | None
    files: Mapping[str, tuple[int, int] | None]
    documents: Mapping[str, str]


@dataclass(frozen=True)
class UpdateSnapshot:
    """The data snapshot one update took, bound to what it captured."""

    operation_id: str
    snapshot_id: str
    stamp: DataStamp
    member_hashes: Mapping[str, str]


def update_snapshot_reason(operation_id: str) -> str:
    """The manifest reason that binds a data snapshot to one update operation."""
    if not operation_id or not operation_id.strip():
        raise ValueError("update operation id must be non-empty")
    return f"update {operation_id}"


def data_stamp(data_dir: Path) -> DataStamp:
    """Stamp the marker, the registered database files and the JSON documents."""
    data_dir = Path(data_dir)
    try:
        marker_file = marker_path(data_dir)
        marker_hash = sha256_file(marker_file) if marker_file.exists() else None
        marker = read_marker(data_dir)
        files: dict[str, tuple[int, int] | None] = {}
        for name in sorted(marker.databases if marker is not None else ()):
            relative = canonical_relative_path(name).as_posix()
            base = canonical_database_path(data_dir, name)
            for suffix in _STAMPED_SUFFIXES:
                path = Path(f"{base}{suffix}")
                try:
                    status = path.stat()
                except FileNotFoundError:
                    files[relative + suffix] = None
                    continue
                # A read-only open of a WAL database leaves an empty WAL behind.
                # An empty journal holds nothing; every write that empties one
                # again has changed the database file itself.
                empty_journal = bool(suffix) and status.st_size == 0
                files[relative + suffix] = (
                    None if empty_journal else (status.st_size, status.st_mtime_ns)
                )
    except OSError as exc:
        raise DatabaseUnavailableError("the data directory could not be stamped") from exc
    return DataStamp(marker=marker_hash, files=files, documents=live_documents(data_dir))


def describe_change(before: DataStamp, after: DataStamp) -> str | None:
    """The first difference between two stamps in words, or ``None``."""
    if before.marker != after.marker:
        return "the data-store marker changed"
    for path in sorted(set(before.files) | set(after.files)):
        if before.files.get(path) != after.files.get(path):
            return f"{path} changed"
    for path in sorted(set(before.documents) | set(after.documents)):
        if before.documents.get(path) != after.documents.get(path):
            return f"the JSON document {path} changed"
    return None


def data_changed_since(data_dir: Path, snapshot: UpdateSnapshot) -> str | None:
    """How the data differs from the moment ``snapshot`` was taken, or ``None``."""
    return describe_change(snapshot.stamp, data_stamp(data_dir))


def create_update_snapshot(
    data_dir: Path, *, operation_id: str, specs: Iterable[DatabaseSpec] = ()
) -> UpdateSnapshot | None:
    """Take the pre-update data snapshot while no server runs on ``data_dir``.

    Returns ``None`` when no database is registered, so there is nothing an
    update could need to restore. Raises ``DatabaseError`` when the snapshot
    cannot be taken or the data changed while it was taken.
    """
    data_dir = Path(data_dir)
    reason = update_snapshot_reason(operation_id)
    marker = read_marker(data_dir)
    if marker is None:
        if any(data_dir.glob("*.db")):
            raise DatabaseFormatError(
                "SQLite databases exist without a current-format data-store marker; "
                "refusing to update without a verified data snapshot"
            )
        return None
    if not marker.databases:
        return None
    missing = sorted(
        name for name in marker.databases if not canonical_database_path(data_dir, name).is_file()
    )
    if missing:
        raise DatabaseUnavailableError(
            "the data-store marker registers missing databases: " + ", ".join(missing)
        )
    before = data_stamp(data_dir)
    created = create_data_snapshot(data_dir, reason=reason, specs=specs)
    if created is None:
        health = read_snapshot_health(data_dir)
        raise DatabaseUnavailableError(
            "the pre-update data snapshot was not verified: "
            + str(health.get("reason") or "no snapshot was published")
        )
    after = data_stamp(data_dir)
    change = describe_change(before, after)
    if change is not None:
        raise DatabaseUnavailableError(
            f"the data changed while the pre-update snapshot was taken ({change}); "
            "another process is writing to the data directory"
        )
    manifest = read_manifest(data_dir, created)
    if manifest is None or manifest.documents is None:
        raise DatabaseCorruptError("the pre-update data snapshot could not be read back")
    if {path: member.sha256 for path, member in manifest.documents.items()} != dict(
        after.documents
    ):
        raise DatabaseUnavailableError(
            "the JSON documents changed while the pre-update snapshot was taken"
        )
    return UpdateSnapshot(
        operation_id=operation_id,
        snapshot_id=manifest.snapshot_id,
        stamp=after,
        member_hashes={name: member.sha256 for name, member in manifest.members.items()},
    )


def restore_update_snapshot(
    data_dir: Path,
    snapshot: UpdateSnapshot,
    *,
    specs: Iterable[DatabaseSpec] = (),
    cause: str = UPDATE_ROLLBACK_CAUSE,
) -> SnapshotRestore:
    """Restore the complete pre-update snapshot after the candidate failed.

    Raises ``UpdateRollbackRefusedError`` without changing anything when the
    snapshot or the data directory fails a precondition. Any other
    ``DatabaseError`` or ``OSError`` comes from the restore itself: the
    maintenance guard then stays until the restore is repeated.
    """
    data_dir = Path(data_dir)
    specs = tuple(specs)
    snapshot_dir = snapshot_root(data_dir) / snapshot.snapshot_id
    try:
        _require_restorable(data_dir, snapshot_dir, snapshot)
        restore_data_snapshot(
            data_dir,
            snapshot_dir,
            specs=specs,
            documents=True,
            retire_unlisted=True,
            cause=cause,
            check_only=True,
        )
    except UpdateRollbackRefusedError:
        raise
    except (DatabaseError, OSError, ValueError) as exc:
        raise UpdateRollbackRefusedError(
            f"the pre-update snapshot {snapshot.snapshot_id} cannot be restored: {exc}"
        ) from exc
    return restore_data_snapshot(
        data_dir,
        snapshot_dir,
        specs=specs,
        documents=True,
        retire_unlisted=True,
        cause=cause,
    )


def _require_restorable(data_dir: Path, snapshot_dir: Path, snapshot: UpdateSnapshot) -> None:
    def refuse(reason: str) -> UpdateRollbackRefusedError:
        return UpdateRollbackRefusedError(
            f"the pre-update snapshot {snapshot.snapshot_id} is not restored automatically: "
            f"{reason}"
        )

    guard = read_maintenance(data_dir)
    if guard is not None:
        raise refuse(f"data maintenance ({guard.operation}) is incomplete")
    manifest = read_manifest(data_dir, snapshot_dir)
    if manifest is None:
        raise refuse("it is missing or malformed")
    if manifest.reason != update_snapshot_reason(snapshot.operation_id):
        raise refuse("it belongs to another operation")
    if manifest.documents is None or {
        path: member.sha256 for path, member in manifest.documents.items()
    } != dict(snapshot.stamp.documents):
        raise refuse("its JSON documents differ from the ones captured for this update")
    if {name: member.sha256 for name, member in manifest.members.items()} != dict(
        snapshot.member_hashes
    ):
        raise refuse("its databases differ from the ones captured for this update")
    marker = read_marker(data_dir)
    if marker is None:
        raise refuse("the data-store marker is missing")
    for name, member in manifest.members.items():
        entry = marker.databases.get(name)
        if entry is None or entry.database_id != member.database_id:
            raise refuse(f"the {name} database registration changed after the snapshot")


def find_update_snapshot(data_dir: Path, operation_id: str) -> str | None:
    """The newest published snapshot id taken for ``operation_id``, if any."""
    reason = update_snapshot_reason(operation_id)
    return next(
        (
            str(summary["snapshot_id"])
            for summary in snapshot_inventory(data_dir)
            if summary["reason"] == reason
        ),
        None,
    )


__all__ = [
    "UPDATE_ROLLBACK_CAUSE",
    "DataStamp",
    "UpdateSnapshot",
    "create_update_snapshot",
    "data_changed_since",
    "data_stamp",
    "describe_change",
    "find_update_snapshot",
    "restore_update_snapshot",
    "update_snapshot_reason",
]
