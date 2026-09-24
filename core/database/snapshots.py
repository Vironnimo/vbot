"""Data snapshots: verified copies of every canonical database in a data directory.

A data snapshot is ``<data-dir>/snapshots/<snapshot-id>/`` holding one
``<name>.db`` copy per canonical database registered in the marker, plus a
strict ``manifest.json``. Each member records its identity, format generation,
applied migrations, size, hash, integrity checks and owner facts. Verification
works per member: a restore candidate for one database needs only that member
to verify. Snapshots are published atomically; retention prunes only after a
verified snapshot was published.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.database._connections import (
    classify_unavailable,
    classify_write_error,
    copy_database,
    readonly_sqlite_uri,
    sqlite_source_id,
)
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseUnavailableError,
)
from core.database.marker import (
    acquire_operation_lock,
    read_marker,
    require_no_maintenance,
    utc_now,
    valid_database_id,
)
from core.database.spec import DatabaseSpec, canonical_database_path, validate_database_name
from core.utils.atomic import atomic_write_text
from core.utils.version import detect_vbot_version

if TYPE_CHECKING:
    from core.database.database import Database

SNAPSHOT_ROOT_NAME = "snapshots"
SNAPSHOT_MANIFEST_NAME = "manifest.json"
SNAPSHOT_HEALTH_FILE_NAME = "health.json"
SNAPSHOT_PARTIAL_SUFFIX = ".partial"
SNAPSHOT_KEEP_COUNT = 5
SNAPSHOT_KEEP_BYTES = 512 * 1024 * 1024
SNAPSHOT_RESERVE_BYTES = 64 * 1024 * 1024
MANIFEST_VERSION = 1
_SNAPSHOT_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = frozenset(
    {
        "manifest_version",
        "snapshot_id",
        "reason",
        "created_at",
        "vbot_version",
        "sqlite_version",
        "sqlite_source_id",
        "members",
        "complete",
    }
)
_MEMBER_KEYS = frozenset(
    {
        "file",
        "database_id",
        "application_id",
        "format_generation",
        "file_size",
        "sha256",
        "integrity",
        "foreign_key_check",
        "migrations",
        "facts",
    }
)


@dataclass(frozen=True)
class SnapshotMember:
    """One verified database copy inside a data snapshot."""

    name: str
    file: str
    database_id: str
    application_id: int
    format_generation: int
    file_size: int
    sha256: str
    migrations: tuple[str, ...]
    facts: Mapping[str, int]


@dataclass(frozen=True)
class SnapshotManifest:
    """Strict metadata describing one complete data snapshot."""

    snapshot_id: str
    reason: str
    created_at: str
    vbot_version: str
    sqlite_version: str
    sqlite_source_id: str
    members: Mapping[str, SnapshotMember]

    @property
    def total_size(self) -> int:
        return sum(member.file_size for member in self.members.values())

    def created_instant(self) -> datetime:
        return _parse_instant(self.created_at)


@dataclass(frozen=True)
class _MemberVerification:
    database_id: str
    application_id: int
    format_generation: int
    migrations: tuple[str, ...]
    facts: Mapping[str, int]


class _SnapshotCancelledError(Exception):
    """Internal cooperative stop signal for an unpublished snapshot."""


def snapshot_root(data_dir: Path) -> Path:
    return Path(data_dir) / SNAPSHOT_ROOT_NAME


def member_file_name(name: str) -> str:
    validate_database_name(name)
    return f"{name}.db"


def _new_snapshot_id() -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}Z-{uuid.uuid4().hex[:8]}"


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp is not timezone-aware")
    return parsed


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path, *, cancelled: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if cancelled is not None and cancelled():
                raise _SnapshotCancelledError
            digest.update(chunk)
    return digest.hexdigest()


def fsync_file(path: Path) -> None:
    # Binary mode: a Windows text-mode open can strip a trailing CTRL-Z.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def _health_path(data_dir: Path) -> Path:
    return snapshot_root(data_dir) / SNAPSHOT_HEALTH_FILE_NAME


def _record_snapshot_health(
    data_dir: Path, state: str, *, reason: str | None = None, snapshot_id: str | None = None
) -> None:
    payload = {
        "state": state,
        "reason": reason,
        "snapshot_id": snapshot_id,
        "observed_at": utc_now(),
    }
    with suppress(OSError):
        atomic_write_text(
            _health_path(data_dir), json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )


def read_snapshot_health(data_dir: Path) -> dict[str, Any]:
    """Return the durable outcome of the latest snapshot attempt."""
    try:
        payload = json.loads(_health_path(data_dir).read_text(encoding="utf-8"))
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


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_member(name: str, payload: object) -> SnapshotMember:
    try:
        validate_database_name(name)
    except ValueError as exc:
        raise DatabaseCorruptError("snapshot manifest lists an invalid database name") from exc
    if not isinstance(payload, dict) or set(payload) != _MEMBER_KEYS:
        raise DatabaseCorruptError(f"snapshot manifest member {name} has an unexpected shape")
    for field in ("application_id", "format_generation", "file_size"):
        if not _is_count(payload[field]):
            raise DatabaseCorruptError(f"snapshot manifest member {name} has an invalid {field}")
    if payload["file"] != member_file_name(name):
        raise DatabaseCorruptError(f"snapshot manifest member {name} names an unexpected file")
    if not valid_database_id(payload["database_id"]):
        raise DatabaseCorruptError(f"snapshot manifest member {name} has an invalid database_id")
    sha256 = payload["sha256"]
    if not isinstance(sha256, str) or _HEX64_PATTERN.fullmatch(sha256) is None:
        raise DatabaseCorruptError(f"snapshot manifest member {name} has an invalid sha256")
    if payload["integrity"] != "ok" or payload["foreign_key_check"] != "ok":
        raise DatabaseCorruptError(f"snapshot manifest member {name} is not verified")
    migrations = payload["migrations"]
    if not isinstance(migrations, list) or not all(
        isinstance(item, str) and item for item in migrations
    ):
        raise DatabaseCorruptError(f"snapshot manifest member {name} has invalid migrations")
    facts = payload["facts"]
    if not isinstance(facts, dict) or not all(
        isinstance(key, str) and key and _is_count(value) for key, value in facts.items()
    ):
        raise DatabaseCorruptError(f"snapshot manifest member {name} has invalid facts")
    return SnapshotMember(
        name=name,
        file=str(payload["file"]),
        database_id=str(payload["database_id"]),
        application_id=int(payload["application_id"]),
        format_generation=int(payload["format_generation"]),
        file_size=int(payload["file_size"]),
        sha256=sha256,
        migrations=tuple(sorted(migrations)),
        facts={str(key): int(value) for key, value in facts.items()},
    )


def _parse_manifest(payload: object, *, child_name: str) -> SnapshotManifest:
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_KEYS:
        raise DatabaseCorruptError("snapshot manifest has an unexpected shape")
    if payload["manifest_version"] != MANIFEST_VERSION or isinstance(
        payload["manifest_version"], bool
    ):
        raise DatabaseCorruptError("snapshot manifest version is unsupported")
    snapshot_id = payload["snapshot_id"]
    if (
        not isinstance(snapshot_id, str)
        or snapshot_id != child_name
        or _SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None
    ):
        raise DatabaseCorruptError("snapshot manifest has an invalid snapshot_id")
    for field in ("reason", "vbot_version", "sqlite_version", "sqlite_source_id"):
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise DatabaseCorruptError(f"snapshot manifest has an invalid {field}")
    created_at = payload["created_at"]
    try:
        if not isinstance(created_at, str):
            raise ValueError("created_at is not text")
        _parse_instant(created_at)
    except ValueError as exc:
        raise DatabaseCorruptError("snapshot manifest has an invalid created_at") from exc
    if payload["complete"] is not True:
        raise DatabaseCorruptError("snapshot manifest is incomplete")
    members = payload["members"]
    if not isinstance(members, dict) or not members:
        raise DatabaseCorruptError("snapshot manifest has no members")
    return SnapshotManifest(
        snapshot_id=snapshot_id,
        reason=str(payload["reason"]),
        created_at=created_at,
        vbot_version=str(payload["vbot_version"]),
        sqlite_version=str(payload["sqlite_version"]),
        sqlite_source_id=str(payload["sqlite_source_id"]),
        members={name: _parse_member(name, member) for name, member in members.items()},
    )


def _manifest_payload(manifest: SnapshotManifest) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "snapshot_id": manifest.snapshot_id,
        "reason": manifest.reason,
        "created_at": manifest.created_at,
        "vbot_version": manifest.vbot_version,
        "sqlite_version": manifest.sqlite_version,
        "sqlite_source_id": manifest.sqlite_source_id,
        "complete": True,
        "members": {
            name: {
                "file": member.file,
                "database_id": member.database_id,
                "application_id": member.application_id,
                "format_generation": member.format_generation,
                "file_size": member.file_size,
                "sha256": member.sha256,
                "integrity": "ok",
                "foreign_key_check": "ok",
                "migrations": list(member.migrations),
                "facts": dict(sorted(member.facts.items())),
            }
            for name, member in sorted(manifest.members.items())
        },
    }


def _safe_snapshot_dir(data_dir: Path, snapshot_dir: Path) -> Path | None:
    root = snapshot_root(data_dir)
    candidate = Path(snapshot_dir)
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve()
    except OSError as exc:
        raise DatabaseUnavailableError("snapshot paths are unavailable") from exc
    if candidate.is_symlink() or candidate_resolved.parent != root_resolved:
        return None
    if candidate.name.startswith("."):
        return None
    return candidate


def member_path(snapshot_dir: Path, member: SnapshotMember) -> Path | None:
    """The member's file inside its snapshot, or ``None`` when it is unsafe or absent."""
    path = Path(snapshot_dir) / member.file
    try:
        if path.is_symlink() or path.resolve().parent != Path(snapshot_dir).resolve():
            return None
        if not stat.S_ISREG(path.stat().st_mode):
            return None
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatabaseUnavailableError(f"snapshot member {member.name} is unavailable") from exc
    return path


def read_manifest(data_dir: Path, snapshot_dir: Path) -> SnapshotManifest | None:
    """Strictly parse one published manifest; ``None`` for anything malformed or unsafe.

    Operational read failures raise ``DatabaseUnavailableError``, so a transient
    problem is never mistaken for a bad snapshot.
    """
    candidate = _safe_snapshot_dir(data_dir, snapshot_dir)
    if candidate is None:
        return None
    manifest_path = candidate / SNAPSHOT_MANIFEST_NAME
    try:
        if manifest_path.is_symlink() or not stat.S_ISREG(manifest_path.stat().st_mode):
            return None
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return _parse_manifest(payload, child_name=candidate.name)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatabaseUnavailableError("snapshot manifest is unavailable") from exc
    except (UnicodeError, json.JSONDecodeError, DatabaseCorruptError):
        return None


def _write_manifest(path: Path, manifest: SnapshotManifest) -> None:
    path.write_text(
        json.dumps(_manifest_payload(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    fsync_file(path)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_database_file(
    path: Path,
    *,
    name: str,
    spec: DatabaseSpec | None = None,
    expected_database_id: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> _MemberVerification:
    """Verify one standalone database copy and read the facts a manifest records.

    Checks the kernel identity against ``name`` (and ``expected_database_id``),
    ``quick_check`` and ``foreign_key_check``. With a ``spec``, also its
    application id and the owner facts it declares.
    """
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(readonly_sqlite_uri(path), uri=True)
        if cancelled is not None:
            connection.set_progress_handler(lambda: int(cancelled()), 10_000)
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        generation = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if spec is not None and application_id != spec.application_id:
            raise DatabaseCorruptError(f"{name} copy has a foreign application_id")
        identity = {
            str(key): str(value)
            for key, value in connection.execute("SELECT key, value FROM kernel_meta")
        }
        database_id = identity.get("database_id")
        if (
            identity.get("database_name") != name
            or identity.get("format_generation") != str(generation)
            or not valid_database_id(database_id)
        ):
            raise DatabaseCorruptError(f"{name} copy has an invalid kernel identity")
        if expected_database_id is not None and database_id != expected_database_id:
            raise DatabaseCorruptError(f"{name} copy has another database identity")
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise DatabaseCorruptError(f"{name} copy integrity failed: {integrity}")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DatabaseCorruptError(f"{name} copy foreign_key_check failed")
        migrations = tuple(
            sorted(str(row[0]) for row in connection.execute("SELECT name FROM kernel_migrations"))
        )
        facts: dict[str, int] = {}
        if spec is not None and spec.snapshot_facts is not None:
            for fact, query in spec.snapshot_facts.queries.items():
                row = connection.execute(query).fetchone()
                facts[fact] = int(row[0]) if row is not None and row[0] is not None else 0
        return _MemberVerification(
            database_id=str(database_id),
            application_id=application_id,
            format_generation=generation,
            migrations=migrations,
            facts=facts,
        )
    except sqlite3.Error as exc:
        if cancelled is not None and cancelled():
            raise _SnapshotCancelledError from exc
        if classify_unavailable(exc) or classify_write_error(exc) == "unavailable":
            raise DatabaseUnavailableError(f"{name} copy is unavailable") from exc
        raise DatabaseCorruptError(f"{name} copy verification failed") from exc
    finally:
        if connection is not None:
            with suppress(BaseException):
                connection.close()


def verify_member(
    snapshot_dir: Path,
    member: SnapshotMember,
    *,
    spec: DatabaseSpec | None = None,
    path: Path | None = None,
) -> None:
    """Verify a member copy and every manifest claim derived from its contents.

    ``path`` verifies another file against the member's claims, such as a
    staged restore copy.
    """
    target = path if path is not None else member_path(snapshot_dir, member)
    if target is None:
        raise DatabaseCorruptError(f"snapshot member {member.name} is missing")
    try:
        if target.stat().st_size != member.file_size:
            raise DatabaseCorruptError(f"snapshot member {member.name} size mismatch")
        if _sha256(target) != member.sha256:
            raise DatabaseCorruptError(f"snapshot member {member.name} hash mismatch")
        verification = verify_database_file(
            target, name=member.name, spec=spec, expected_database_id=member.database_id
        )
    except FileNotFoundError as exc:
        raise DatabaseCorruptError(f"snapshot member {member.name} is missing") from exc
    except OSError as exc:
        raise DatabaseUnavailableError(f"snapshot member {member.name} is unavailable") from exc
    if (
        verification.application_id != member.application_id
        or verification.format_generation != member.format_generation
        or verification.migrations != member.migrations
    ):
        raise DatabaseCorruptError(f"snapshot member {member.name} disagrees with its manifest")
    for fact, value in verification.facts.items():
        if fact in member.facts and member.facts[fact] != value:
            raise DatabaseCorruptError(f"snapshot member {member.name} fact {fact} mismatch")


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
    """Verify every member of one snapshot; operational failures still raise."""
    manifest = read_manifest(data_dir, snapshot_dir)
    if manifest is None or _foreign(manifest, expected):
        return None
    try:
        for name, member in manifest.members.items():
            verify_member(snapshot_dir, member, spec=(specs or {}).get(name))
    except DatabaseCorruptError:
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
        except DatabaseCorruptError:
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
    except (OSError, DatabaseUnavailableError):
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
) -> Path | None:
    """Capture, verify and atomically publish one snapshot of every registered database.

    ``databases`` are the open handles of this process: their members are copied
    online through the handle, so live writers keep committing. Every other
    registered database is copied from its file, which must not be written by
    another process meanwhile. ``specs`` add owner facts for members not open
    here. Returns ``None`` when there is nothing to capture, the attempt was
    cancelled, or it failed; failures are recorded in the snapshot health.
    Refuses with ``DatabaseFormatError`` while data maintenance is incomplete.
    """
    data_dir = Path(data_dir)
    if not reason or not reason.strip():
        raise ValueError("snapshot reason must be non-empty")
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
        if cancelled is not None and cancelled():
            raise _SnapshotCancelledError
        try:
            root.mkdir(parents=True, exist_ok=True)
            needed = sum(
                canonical_database_path(data_dir, name).stat().st_size
                for name in marker.databases
                if canonical_database_path(data_dir, name).exists()
            )
            if shutil.disk_usage(root).free < needed + SNAPSHOT_RESERVE_BYTES:
                _record_snapshot_health(
                    data_dir, "degraded", reason="insufficient snapshot reserve"
                )
                return None
        except OSError:
            _record_snapshot_health(data_dir, "degraded", reason="snapshot capacity probe failed")
            return None
        snapshot_id = _new_snapshot_id()
        partial = root / f".{snapshot_id}.{os.getpid()}{SNAPSHOT_PARTIAL_SUFFIX}"
        partial.mkdir(parents=False, exist_ok=False)
        members: dict[str, SnapshotMember] = {}
        for name, entry in sorted(marker.databases.items()):
            destination = partial / member_file_name(name)
            source_path = canonical_database_path(data_dir, name)
            if not source_path.is_file():
                raise DatabaseUnavailableError(f"registered database {name} is missing")
            handle = open_databases.get(name)
            if handle is not None:
                copied = handle.backup(destination, cancelled=cancelled)
            else:
                copied = _copy_file_database(source_path, destination, cancelled=cancelled)
            if not copied or (cancelled is not None and cancelled()):
                raise _SnapshotCancelledError
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
            created_at=utc_now(),
            vbot_version=detect_vbot_version(),
            sqlite_version=sqlite3.sqlite_version,
            sqlite_source_id=sqlite_source_id() or "unknown",
            members=members,
        )
        _write_manifest(partial / SNAPSHOT_MANIFEST_NAME, manifest)
        fsync_dir(partial)
        if cancelled is not None and cancelled():
            raise _SnapshotCancelledError
        final = root / snapshot_id
        os.replace(partial, final)
        partial = None
        fsync_dir(root)
        _prune_snapshots(data_dir, protected_snapshot=final)
        _record_snapshot_health(data_dir, "healthy", snapshot_id=snapshot_id)
        return final
    except _SnapshotCancelledError:
        return None
    except (OSError, sqlite3.Error, DatabaseCorruptError, DatabaseUnavailableError) as exc:
        _record_snapshot_health(data_dir, "degraded", reason=f"{type(exc).__name__}: {exc}")
        return None
    finally:
        if partial is not None:
            shutil.rmtree(partial, ignore_errors=True)
        lock.release()


def _copy_file_database(
    source_path: Path, destination: Path, *, cancelled: Callable[[], bool] | None
) -> bool:
    """Copy a database that is not open in this process, without changing it."""
    with closing(
        sqlite3.connect(readonly_sqlite_uri(source_path), uri=True, isolation_level=None)
    ) as source:
        return copy_database(source, destination, cancelled=cancelled)


def _prune_snapshots(data_dir: Path, *, protected_snapshot: Path) -> None:
    """Apply count and byte retention around the snapshot just published."""
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
    for child, manifest in others:
        if retained >= SNAPSHOT_KEEP_COUNT or total + manifest.total_size > SNAPSHOT_KEEP_BYTES:
            shutil.rmtree(child, ignore_errors=True)
        else:
            retained += 1
            total += manifest.total_size
