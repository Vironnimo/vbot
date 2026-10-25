"""Data snapshots: verified copies of the durable data in a data directory.

A data snapshot is ``<data-dir>/snapshots/<snapshot-id>/`` holding one
``<name>.db`` copy per canonical database registered in the marker, the JSON
document set under ``documents/`` (see ``core.database._documents``), and a
``manifest.json``. Each database member records its identity, format
generation, applied migrations, size, hash, integrity checks and owner facts;
each document records its relative path, size and hash. A reader requires every
field it knows, ignores fields and documents it does not know (a newer vBot may
add them), and refuses another ``manifest_version``, so snapshots taken by a
newer vBot of the same manifest version stay usable for automatic restore and
update rollback of an older one. Verification works per database member: a
restore candidate for one database needs only that member to verify. A snapshot
as a whole verifies only when every database member and every document does.
Snapshots are published atomically; retention prunes only after a verified
snapshot was published.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

# A module import: ``core.json_documents`` imports the kernel's snapshot freeze in turn.
import core.json_documents as json_documents
from core.database._connections import (
    copy_database_file,
    readonly_sqlite_uri,
    sqlite_source_id,
)
from core.database._documents import (
    DOCUMENTS_DIRECTORY_NAME,
    DocumentMember,
    capture_documents,
    documents_present,
    documents_size,
    sync_documents,
    verify_documents,
)
from core.database._files import fsync_dir, fsync_file
from core.database._snapshot_manifest import (
    MANIFEST_VERSION as MANIFEST_VERSION,
)
from core.database._snapshot_manifest import (
    SNAPSHOT_MANIFEST_NAME as SNAPSHOT_MANIFEST_NAME,
)
from core.database._snapshot_manifest import (
    SNAPSHOT_ROOT_NAME as SNAPSHOT_ROOT_NAME,
)
from core.database._snapshot_manifest import (
    SnapshotManifest as SnapshotManifest,
)
from core.database._snapshot_manifest import (
    SnapshotMember as SnapshotMember,
)
from core.database._snapshot_manifest import (
    _write_manifest,
)
from core.database._snapshot_manifest import (
    member_file_name as member_file_name,
)
from core.database._snapshot_manifest import (
    member_path as member_path,
)
from core.database._snapshot_manifest import (
    read_manifest as read_manifest,
)
from core.database._snapshot_manifest import (
    snapshot_root as snapshot_root,
)
from core.database._snapshot_retention import (
    release_snapshot_retention as release_snapshot_retention,
)
from core.database._snapshot_retention import (
    retain_snapshot_locked,
    retained_snapshot_ids,
)
from core.database._snapshot_verification import (
    UNUSABLE_COPY_ERRORS as UNUSABLE_COPY_ERRORS,
)
from core.database._snapshot_verification import (
    _sha256,
    _SnapshotCancelledError,
)
from core.database._snapshot_verification import (
    verify_database_file as verify_database_file,
)
from core.database._snapshot_verification import (
    verify_member as verify_member,
)
from core.database._staging import remove_staging_locked, snapshot_staging_path
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseUnavailableError,
)
from core.database.marker import (
    acquire_operation_lock,
    describe_missing_databases,
    missing_database_reason,
    operation_lock,
    read_marker,
    require_no_maintenance,
)
from core.database.snapshot_barrier import capture_members
from core.database.spec import (
    DatabaseSpec,
    canonical_database_path,
    validate_database_name,
)
from core.utils.atomic import atomic_write_text, replace_path
from core.utils.file_status import exists_strict, is_file_strict, is_link_status
from core.utils.ids import is_safe_id
from core.utils.timestamps import (
    is_canonical_timestamp,
    utc_now_timestamp,
)
from core.utils.tree_move import remove_tree
from core.utils.version import detect_vbot_version

if TYPE_CHECKING:
    from core.database.database import Database
    from core.database.snapshot_barrier import SnapshotBarrier

_LOGGER = logging.getLogger("vbot.database")

SNAPSHOT_HEALTH_FILE_NAME = "health.json"
SNAPSHOT_KEEP_COUNT = 5
#: Retention's byte budget. It never prunes below :data:`SNAPSHOT_KEEP_MINIMUM`
#: snapshots, so a Session history that outgrows the budget still keeps a fallback.
SNAPSHOT_KEEP_BYTES = 10 * 1024 * 1024 * 1024
SNAPSHOT_KEEP_MINIMUM = 2
SNAPSHOT_RESERVE_BYTES = 64 * 1024 * 1024


def _new_snapshot_id() -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}Z-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def _health_path(data_dir: Path) -> Path:
    return snapshot_root(data_dir) / SNAPSHOT_HEALTH_FILE_NAME


def _record_snapshot_health(
    data_dir: Path, state: str, *, reason: str | None = None, snapshot_id: str | None = None
) -> None:
    """Record the latest attempt's outcome and log only a change of health.

    A degradation logs once per reason, not on every failed attempt; the first
    healthy attempt after it logs the recovery.
    """
    previous = read_snapshot_health(data_dir)
    payload = {
        "state": state,
        "reason": reason,
        "snapshot_id": snapshot_id,
        "observed_at": utc_now_timestamp(),
    }
    with suppress(OSError):
        atomic_write_text(
            _health_path(data_dir), json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
    if state == "degraded":
        repeated = previous["state"] == "degraded" and previous["reason"] == reason
        _LOGGER.log(
            logging.DEBUG if repeated else logging.WARNING,
            "Data snapshot health degraded: %s",
            reason,
        )
    elif previous["state"] == "degraded":
        _LOGGER.info("Data snapshot health recovered (snapshot=%s)", snapshot_id)


def read_snapshot_health(data_dir: Path) -> dict[str, Any]:
    """Return the durable outcome of the latest snapshot attempt."""
    try:
        payload = json.loads(_health_path(data_dir).read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        return {"state": "unknown", "reason": None, "snapshot_id": None, "observed_at": None}
    if (
        not isinstance(payload, dict)
        or payload.get("state") not in {"healthy", "degraded"}
        # The record is written with a canonical time; any other is damage.
        or not is_canonical_timestamp(payload.get("observed_at"))
    ):
        return {
            "state": "degraded",
            "reason": "snapshot health record is malformed",
            "snapshot_id": None,
            "observed_at": None,
        }
    return payload


def _foreign(manifest: SnapshotManifest, expected: Mapping[str, str] | None) -> bool:
    if expected is None:
        return False
    return any(
        name in expected and member.database_id != expected[name]
        for name, member in manifest.members.items()
    )


def _published_children(data_dir: Path) -> list[Path]:
    root = snapshot_root(data_dir)
    try:
        if not stat.S_ISDIR(root.stat().st_mode):
            return []
        children = list(root.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise DatabaseUnavailableError("snapshot inventory is unavailable") from exc
    return [
        child
        for child in children
        if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
    ]


def read_retained_snapshot_members[Result](
    data_dir: Path,
    name: str,
    read: Callable[[sqlite3.Connection], Result],
) -> list[Result]:
    """Read one owner's member from every retained snapshot without changing it.

    A resource collector uses these copies as additional roots. Hold the
    operation lock so publication/pruning cannot race the inventory, and fail
    closed on an unreadable or malformed published snapshot. This is not a
    restore verification: immutable published copies were verified at capture;
    the read checks identity and schema, without rehashing every database.
    """
    validate_database_name(name)
    results: list[Result] = []
    with operation_lock(data_dir):
        try:
            children = list(snapshot_root(data_dir).iterdir())
        except FileNotFoundError:
            return results
        except OSError as exc:
            raise DatabaseUnavailableError("snapshot inventory is unavailable") from exc
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                status = child.lstat()
                if is_link_status(status) or not stat.S_ISDIR(status.st_mode):
                    continue
                manifest = read_manifest(data_dir, child)
                if manifest is None:
                    raise DatabaseCorruptError("retained snapshot manifest is invalid")
                member = manifest.members.get(name)
                if member is None:
                    continue
                path = member_path(child, member)
                if path is None:
                    raise DatabaseCorruptError(f"retained snapshot member {name} is missing")
                with closing(sqlite3.connect(readonly_sqlite_uri(path), uri=True)) as connection:
                    connection.row_factory = sqlite3.Row
                    identity = dict(connection.execute("SELECT key, value FROM kernel_meta"))
                    migrations = tuple(
                        sorted(
                            str(row[0])
                            for row in connection.execute("SELECT name FROM kernel_migrations")
                        )
                    )
                    if (
                        identity.get("database_name") != name
                        or identity.get("database_id") != member.database_id
                        or int(connection.execute("PRAGMA application_id").fetchone()[0])
                        != member.application_id
                        or int(connection.execute("PRAGMA user_version").fetchone()[0])
                        != member.format_generation
                        or migrations != member.migrations
                    ):
                        raise DatabaseCorruptError(f"retained snapshot member {name} has changed")
                    results.append(read(connection))
            except (OSError, sqlite3.Error) as exc:
                raise DatabaseUnavailableError(
                    f"retained snapshot member {name} is unavailable"
                ) from exc
    return results


def verified_snapshots(
    data_dir: Path,
    *,
    specs: Mapping[str, DatabaseSpec] | None = None,
    expected: Mapping[str, str] | None = None,
) -> list[tuple[Path, SnapshotManifest]]:
    """Every published snapshot whose members all verify, newest first."""
    verified: list[tuple[datetime, Path, SnapshotManifest]] = []
    for child in _published_children(data_dir):
        manifest = read_verified_manifest(data_dir, child, specs=specs, expected=expected)
        if manifest is not None:
            verified.append((manifest.created_instant(), child, manifest))
    verified.sort(key=lambda item: item[0], reverse=True)
    return [(path, manifest) for _instant, path, manifest in verified]


def read_verified_manifest(
    data_dir: Path,
    snapshot_dir: Path,
    *,
    specs: Mapping[str, DatabaseSpec] | None = None,
    expected: Mapping[str, str] | None = None,
) -> SnapshotManifest | None:
    """Verify every member and document of one snapshot; operational failures still raise."""
    manifest = read_manifest(data_dir, snapshot_dir)
    if manifest is None or _foreign(manifest, expected):
        return None
    try:
        for name, member in manifest.members.items():
            verify_member(snapshot_dir, member, spec=(specs or {}).get(name))
        verify_documents(snapshot_dir, manifest.documents)
    except UNUSABLE_COPY_ERRORS:
        return None
    return manifest


def member_restore_candidates(
    data_dir: Path,
    name: str,
    *,
    database_id: str,
    spec: DatabaseSpec | None = None,
) -> list[tuple[Path, SnapshotManifest, SnapshotMember]]:
    """Snapshots whose ``name`` member has ``database_id`` and verifies, newest first."""
    candidates: list[tuple[datetime, Path, SnapshotManifest, SnapshotMember]] = []
    for child in _published_children(data_dir):
        manifest = read_manifest(data_dir, child)
        member = None if manifest is None else manifest.members.get(name)
        if manifest is None or member is None or member.database_id != database_id:
            continue
        try:
            verify_member(child, member, spec=spec)
        except UNUSABLE_COPY_ERRORS:
            continue
        candidates.append((manifest.created_instant(), child, manifest, member))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [(path, manifest, member) for _instant, path, manifest, member in candidates]


def snapshot_summary(manifest: SnapshotManifest) -> dict[str, Any]:
    """Operator-safe snapshot metadata; never database content."""
    return {
        "snapshot_id": manifest.snapshot_id,
        "created_at": manifest.created_at,
        "reason": manifest.reason,
        "vbot_version": manifest.vbot_version,
        "file_size": manifest.total_size,
        "members": {
            name: {
                "database_id": member.database_id,
                "format_generation": member.format_generation,
                "file_size": member.file_size,
                "sha256": member.sha256,
                "migrations": len(member.migrations),
                "facts": dict(sorted(member.facts.items())),
            }
            for name, member in sorted(manifest.members.items())
        },
        "documents": {
            "count": len(manifest.documents),
            "file_size": sum(document.file_size for document in manifest.documents.values()),
        },
    }


def list_data_snapshots(
    data_dir: Path,
    *,
    specs: Mapping[str, DatabaseSpec] | None = None,
    expected: Mapping[str, str] | None = None,
) -> list[Path]:
    """Fixed-root snapshots with a strict manifest and fully verified members."""
    return [
        path for path, _manifest in verified_snapshots(data_dir, specs=specs, expected=expected)
    ]


def snapshot_summaries(
    data_dir: Path,
    *,
    specs: Mapping[str, DatabaseSpec] | None = None,
    expected: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Verified snapshot metadata, newest first."""
    return [
        snapshot_summary(manifest)
        for _path, manifest in verified_snapshots(data_dir, specs=specs, expected=expected)
    ]


def snapshot_inventory(
    data_dir: Path, *, expected: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Strict manifests whose member files have the recorded size, without rehashing.

    For status views: cheap enough to call often on multi-GB snapshots.
    """
    entries: list[tuple[datetime, dict[str, Any]]] = []
    try:
        children = _published_children(data_dir)
    except DatabaseUnavailableError:
        return []
    for child in children:
        manifest = _shallow_manifest(data_dir, child)
        if manifest is None or _foreign(manifest, expected):
            continue
        entries.append((manifest.created_instant(), snapshot_summary(manifest)))
    entries.sort(key=lambda item: item[0], reverse=True)
    return [summary for _instant, summary in entries]


def read_snapshot_summary(data_dir: Path, snapshot_dir: Path) -> dict[str, Any] | None:
    """One strict manifest with matching member sizes, without deep verification."""
    manifest = _shallow_manifest(data_dir, snapshot_dir)
    return None if manifest is None else snapshot_summary(manifest)


def _shallow_manifest(data_dir: Path, snapshot_dir: Path) -> SnapshotManifest | None:
    try:
        manifest = read_manifest(data_dir, snapshot_dir)
        if manifest is None:
            return None
        for member in manifest.members.values():
            path = member_path(snapshot_dir, member)
            if path is None or path.stat().st_size != member.file_size:
                return None
        if not documents_present(snapshot_dir, manifest.documents):
            return None
    except OSError, DatabaseUnavailableError:
        return None
    return manifest


# ---------------------------------------------------------------------------
# Creation and retention
# ---------------------------------------------------------------------------


def create_data_snapshot(
    data_dir: Path,
    *,
    reason: str,
    databases: Iterable[Database] = (),
    specs: Iterable[DatabaseSpec] = (),
    cancelled: Callable[[], bool] | None = None,
    barrier: SnapshotBarrier | None = None,
    retain_as: str | None = None,
) -> Path | None:
    """Capture, verify and atomically publish one snapshot of the durable data.

    Every registered database and the JSON document set are captured by
    ``capture_members`` (``core.database.snapshot_barrier``), so the copies show
    the data a crash at one instant would leave; they are made durable after
    it. ``databases`` are this process's open handles, copied online; any other
    registered database is copied from its file, which no other process may
    write meanwhile. A mutating process passes its ``barrier``; ``specs`` add
    owner facts and capture roles for members not open here. Returns ``None``
    when there is nothing to capture, or the attempt was cancelled or failed
    (recorded in the snapshot health, a missing database file with the
    operator's next step). Refuses with ``DatabaseFormatError`` during maintenance.
    """
    data_dir = Path(data_dir)
    if not reason or not reason.strip():
        raise ValueError("snapshot reason must be non-empty")
    if retain_as is not None and not is_safe_id(retain_as):
        raise ValueError("invalid snapshot retention owner")
    require_no_maintenance(data_dir)
    marker = read_marker(data_dir)
    if marker is None or not marker.databases:
        return None
    open_databases = {database.name: database for database in databases}
    known_specs = {spec.name: spec for spec in specs}
    known_specs.update({name: database.spec for name, database in open_databases.items()})
    lock = acquire_operation_lock(data_dir, cancelled=cancelled)
    if lock is None:
        return None
    root = snapshot_root(data_dir)
    partial: Path | None = None
    try:
        require_no_maintenance(data_dir)
        if cancelled is not None and cancelled():
            raise _SnapshotCancelledError
        # Registrations may have changed while this attempt waited for the lock.
        marker = read_marker(data_dir)
        if marker is None or not marker.databases:
            return None
        # Under the lock every staging is a killed attempt's: its space comes
        # back before a verified snapshot has to make room.
        remove_staging_locked(data_dir, snapshot_root=root)
        missing = describe_missing_databases(data_dir, marker)
        if missing is not None:
            _record_snapshot_health(data_dir, "degraded", reason=missing)
            return None
        try:
            root.mkdir(parents=True, exist_ok=True)
            needed = sum(
                canonical_database_path(data_dir, name).stat().st_size
                for name in marker.databases
                if exists_strict(canonical_database_path(data_dir, name))
            ) + documents_size(data_dir)
            if not _make_room(data_dir, needed):
                _record_snapshot_health(
                    data_dir, "degraded", reason="insufficient snapshot reserve"
                )
                return None
        except OSError:
            _record_snapshot_health(data_dir, "degraded", reason="snapshot capacity probe failed")
            return None
        snapshot_id = _new_snapshot_id()
        partial = staging = snapshot_staging_path(root, snapshot_id)
        partial.mkdir(parents=False, exist_ok=False)
        documents: dict[str, DocumentMember] | None = None

        def copy_member(name: str) -> None:
            source_path = canonical_database_path(data_dir, name)
            if not is_file_strict(source_path):
                raise DatabaseUnavailableError(missing_database_reason(name))
            destination = staging / member_file_name(name)
            handle = open_databases.get(name)
            if handle is not None:
                copied = handle.backup(destination, cancelled=cancelled)
            else:
                copied = copy_database_file(source_path, destination, cancelled=cancelled)
            if not copied or (cancelled is not None and cancelled()):
                raise _SnapshotCancelledError

        def copy_documents() -> None:
            nonlocal documents
            if (documents := capture_documents(data_dir, staging, cancelled=cancelled)) is None:
                raise _SnapshotCancelledError

        def discard_copies() -> None:
            # Copies keep their source's bits; read-only ones are removed too.
            remove_tree(staging, within=root)
            staging.mkdir()

        capture = capture_members(
            data_dir,
            {name: known_specs.get(name) for name in marker.databases},
            copy_database=copy_member,
            copy_documents=copy_documents,
            discard_copies=discard_copies,
            barrier=barrier,
            cancelled=cancelled,
        )
        if capture is None or documents is None:
            raise _SnapshotCancelledError
        sync_documents(staging, documents)
        members: dict[str, SnapshotMember] = {}
        for name, entry in sorted(marker.databases.items()):
            destination = partial / member_file_name(name)
            file_size = destination.stat().st_size
            digest = _sha256(destination, cancelled=cancelled)
            verification = verify_database_file(
                destination,
                name=name,
                spec=known_specs.get(name),
                expected_database_id=entry.database_id,
                cancelled=cancelled,
            )
            if verification.format_generation != entry.format_generation:
                raise DatabaseCorruptError(f"{name} generation disagrees with the marker")
            fsync_file(destination)
            members[name] = SnapshotMember(
                name=name,
                file=destination.name,
                database_id=verification.database_id,
                application_id=verification.application_id,
                format_generation=verification.format_generation,
                file_size=file_size,
                sha256=digest,
                migrations=verification.migrations,
                facts=verification.facts,
            )
        manifest = SnapshotManifest(
            snapshot_id=snapshot_id,
            reason=reason,
            created_at=utc_now_timestamp(),
            vbot_version=detect_vbot_version(),
            sqlite_version=sqlite3.sqlite_version,
            sqlite_source_id=sqlite_source_id() or "unknown",
            members=members,
            documents=documents,
        )
        _write_manifest(partial / SNAPSHOT_MANIFEST_NAME, manifest)
        fsync_dir(partial)
        if cancelled is not None and cancelled():
            raise _SnapshotCancelledError
        final = root / snapshot_id
        replace_path(partial, final)
        partial = None
        fsync_dir(root)
        if retain_as is not None:
            retain_snapshot_locked(data_dir, retain_as, snapshot_id)
        _prune_snapshots(data_dir, protected_snapshot=final)
        _LOGGER.info(
            "Created data snapshot (snapshot=%s databases=%d documents=%d reason=%s "
            "held_ms=%.0f waited_changes=%d attempts=%d)",
            snapshot_id,
            len(members),
            len(documents),
            reason,
            capture.held_seconds * 1000,
            capture.waited_changes,
            capture.attempts,
        )
        _record_snapshot_health(data_dir, "healthy", snapshot_id=snapshot_id)
        return final
    except _SnapshotCancelledError:
        return None
    except (OSError, sqlite3.Error, DatabaseUnavailableError, *UNUSABLE_COPY_ERRORS) as exc:
        _record_snapshot_health(data_dir, "degraded", reason=f"{type(exc).__name__}: {exc}")
        return None
    finally:
        if partial is not None:
            with suppress(OSError):
                remove_tree(partial, within=root)
        lock.release()


def remove_interrupted_staging(data_dir: Path) -> None:
    """Remove the staging that killed snapshots, restores and database creations left.

    Every Runtime start calls it, so such copies never outlast the next start;
    every snapshot does the same before it makes room. Raises
    ``DatabaseUnavailableError`` while the operation lock is busy or unavailable.
    """
    data_dir = Path(data_dir)
    with operation_lock(data_dir):
        remove_staging_locked(data_dir, snapshot_root=snapshot_root(data_dir))


def remove_retired_document_copies(data_dir: Path, patterns: Iterable[str]) -> int:
    """Delete the copies of documents this vBot no longer keeps from every data snapshot.

    For plaintext credentials an older vBot captured (``oauth/*.json`` before the
    Secret Store). A reader leaves out documents it does not know, so such a
    copy is neither verified nor restored any more and every snapshot stays
    usable; a pattern of the current document set is refused. Returns how many
    copies were deleted. Raises ``DatabaseUnavailableError`` while the operation
    lock is busy or a snapshot cannot be listed.
    """
    selected = tuple(patterns)
    if not selected or any(
        pattern in json_documents.SNAPSHOT_DOCUMENTS.values() for pattern in selected
    ):
        raise ValueError("only documents outside the snapshot document set can be removed")
    data_dir = Path(data_dir)
    removed = 0
    with operation_lock(data_dir):
        for child in _published_children(data_dir):
            documents = child / DOCUMENTS_DIRECTORY_NAME
            try:
                paths = json_documents.matching_data_paths(documents, selected)
            except OSError as exc:
                raise DatabaseUnavailableError(f"snapshot {child.name} cannot be listed") from exc
            directories: set[Path] = set()
            for path in paths:
                copy = documents.joinpath(*path.split("/"))
                copy.unlink(missing_ok=True)
                directories.add(copy.parent)
                removed += 1
            for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
                with suppress(OSError):
                    directory.rmdir()
    if removed:
        _LOGGER.info("Deleted retired document copies from data snapshots (copies=%d)", removed)
    return removed


def _make_room(data_dir: Path, needed: int) -> bool:
    """Whether ``needed`` bytes fit beside the reserve, removing the oldest snapshots for them.

    A new snapshot is worth more than the oldest retained one, so a full disk
    never blocks a snapshot (and the update that takes it) while older ones
    could give way. The newest snapshot always stays. Raises ``OSError`` when
    the free space cannot be read or a snapshot cannot be removed.
    """
    root = snapshot_root(data_dir)
    retained = retained_snapshot_ids(data_dir)
    if shutil.disk_usage(root).free >= needed + SNAPSHOT_RESERVE_BYTES:
        return True
    try:
        children = _published_children(data_dir)
    except DatabaseUnavailableError:
        return False
    oldest_first = sorted(
        (
            (child, manifest)
            for child in children
            if (manifest := _shallow_manifest(data_dir, child)) is not None
        ),
        key=lambda item: (item[1].created_instant(), item[1].snapshot_id),
    )
    removed: list[str] = []
    try:
        while shutil.disk_usage(root).free < needed + SNAPSHOT_RESERVE_BYTES:
            if len(oldest_first) <= 1:
                return False
            removable = next(
                (
                    index
                    for index, (_, item) in enumerate(oldest_first[:-1])
                    if item.snapshot_id not in retained
                ),
                None,
            )
            if removable is None:
                return False
            child, manifest = oldest_first.pop(removable)
            remove_tree(child, within=root)
            removed.append(manifest.snapshot_id)
    finally:
        if removed:
            _LOGGER.info(
                "Removed the oldest data snapshots to make room for a new one (snapshots=%s)",
                ",".join(removed),
            )
    return True


def _prune_snapshots(data_dir: Path, *, protected_snapshot: Path) -> None:
    """Apply count and byte retention around the snapshot just published."""
    retained_ids = retained_snapshot_ids(data_dir)
    try:
        children = _published_children(data_dir)
    except DatabaseUnavailableError:
        return
    entries = [
        (child, manifest)
        for child in children
        if (manifest := _shallow_manifest(data_dir, child)) is not None
    ]
    protected = next((manifest for child, manifest in entries if child == protected_snapshot), None)
    if protected is None:
        return
    others = sorted(
        ((child, manifest) for child, manifest in entries if child != protected_snapshot),
        key=lambda item: (item[1].created_instant(), item[1].snapshot_id),
        reverse=True,
    )
    total = protected.total_size
    retained = 1
    root = snapshot_root(data_dir)
    for child, manifest in others:
        over_budget = total + manifest.total_size > SNAPSHOT_KEEP_BYTES
        if manifest.snapshot_id not in retained_ids and (
            retained >= SNAPSHOT_KEEP_COUNT or (over_budget and retained >= SNAPSHOT_KEEP_MINIMUM)
        ):
            with suppress(OSError):
                remove_tree(child, within=root)
        else:
            retained += 1
            total += manifest.total_size
