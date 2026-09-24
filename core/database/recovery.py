"""Quarantine, recovery incidents and per-member restore of canonical databases.

Recovery replaces one canonical database at a time from a verified data
snapshot member. The damaged bundle (database file plus sidecars) is moved to
``<data-dir>/quarantine/<name>/<timestamp>-<id>/`` and never deleted. Every
restore publishes a durable incident at ``<data-dir>/incidents/<name>.json``:
first as ``pending`` before anything is replaced, then as ``ok`` once the
restored file verified. A crash between the two resumes on the next open.

Automatic restore runs only when opening a registered database finds it
missing, damaged or with another identity; never for a busy or locked file and
never over a database a newer vBot changed. An operator restore of a whole data
snapshot also holds the maintenance guard, so Runtime refuses a half-restored
data directory.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from core.database._connections import (
    classified_error,
    database_files,
    has_live_connection,
    readonly_sqlite_uri,
)
from core.database._schema import KERNEL_SCHEMA_SQL, declared_schema, schema_changes
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    IncidentConflictError,
)
from core.database.marker import (
    acquire_operation_lock,
    maintenance,
    read_marker,
    utc_now,
    valid_database_id,
)
from core.database.snapshots import (
    SnapshotManifest,
    SnapshotMember,
    fsync_dir,
    fsync_file,
    member_path,
    member_restore_candidates,
    read_manifest,
    snapshot_root,
    verify_database_file,
    verify_member,
)
from core.database.spec import (
    DatabaseSpec,
    canonical_database_path,
    validate_database_name,
)

QUARANTINE_ROOT_NAME = "quarantine"
INCIDENT_ROOT_NAME = "incidents"
RESTORE_OPERATION = "restore"
_INCIDENT_KEYS = frozenset(
    {
        "incident_id",
        "database",
        "cause",
        "quarantine",
        "restored_snapshot_id",
        "restored_snapshot_time",
        "recovered_at",
        "verification",
        "possible_loss_interval",
        "acknowledged",
    }
)


@dataclass(frozen=True)
class QuarantineResult:
    """Outcome of moving one database bundle to quarantine."""

    status: Literal["no_bundle", "success", "failed"]
    path: Path | None = None
    reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    @property
    def had_bundle(self) -> bool:
        return self.status != "no_bundle"


@dataclass(frozen=True)
class _Probe:
    usable: bool
    recoverable: bool
    cause: str
    detected_at: str


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


def quarantine_root(data_dir: Path) -> Path:
    return Path(data_dir) / QUARANTINE_ROOT_NAME


def _new_quarantine_path(data_dir: Path, name: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return quarantine_root(data_dir) / name / f"{stamp}-{uuid.uuid4().hex[:8]}"


def _bundle(path: Path) -> list[Path]:
    return [member for member in database_files(path) if member.exists()]


def _quarantine_bundle(
    data_dir: Path, name: str, path: Path, *, destination: Path | None = None
) -> QuarantineResult:
    """Move a complete bundle, or roll back every member on the first failure."""
    members = _bundle(path)
    if not members:
        return QuarantineResult("no_bundle")
    batch = destination or _new_quarantine_path(data_dir, name)
    try:
        batch.parent.mkdir(parents=True, exist_ok=True)
        batch.mkdir()
    except OSError as exc:
        return QuarantineResult("failed", reason=str(exc))
    moved: list[tuple[Path, Path]] = []
    try:
        for member in members:
            target = batch / member.name
            os.replace(member, target)
            moved.append((member, target))
        fsync_dir(batch)
        fsync_dir(batch.parent)
        fsync_dir(path.parent)
    except OSError as exc:
        rollback_errors: list[str] = []
        for original, target in reversed(moved):
            try:
                os.replace(target, original)
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


def quarantine_database(data_dir: Path, name: str) -> QuarantineResult:
    """Move one canonical database bundle to quarantine for an explicit recovery."""
    validate_database_name(name)
    return _quarantine_bundle(Path(data_dir), name, canonical_database_path(data_dir, name))


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


def incident_path(data_dir: Path, name: str) -> Path:
    validate_database_name(name)
    return Path(data_dir) / INCIDENT_ROOT_NAME / f"{name}.json"


def _write_json_durably(path: Path, payload: Mapping[str, Any], *, what: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        fsync_file(temporary)
        os.replace(temporary, path)
        fsync_dir(path.parent)
    except OSError as exc:
        raise DatabaseUnavailableError(f"{what} could not be durably published: {path}") from exc
    finally:
        with suppress(OSError):
            temporary.unlink()


def write_incident(
    data_dir: Path,
    name: str,
    *,
    cause: str,
    quarantine_path: Path | str | None,
    restored_snapshot_id: str,
    restored_snapshot_time: str,
    failure_detected_at: str | None = None,
    verification: Literal["pending", "ok"] = "ok",
    incident_id: str | None = None,
    recovered_at: str | None = None,
) -> dict[str, Any]:
    """Publish the durable recovery incident of one database, or raise."""
    detected_at = failure_detected_at or utc_now()
    payload: dict[str, Any] = {
        "incident_id": incident_id or uuid.uuid4().hex,
        "database": name,
        "cause": cause,
        "quarantine": str(quarantine_path) if quarantine_path else None,
        "restored_snapshot_id": restored_snapshot_id,
        "restored_snapshot_time": restored_snapshot_time,
        "recovered_at": None if verification == "pending" else (recovered_at or utc_now()),
        "verification": verification,
        "possible_loss_interval": {"start": restored_snapshot_time, "end": detected_at},
        "acknowledged": False,
    }
    _write_json_durably(incident_path(data_dir, name), payload, what="recovery incident")
    return payload


def _valid_instant(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _parse_incident(payload: object, name: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _INCIDENT_KEYS:
        raise DatabaseCorruptError(f"the {name} recovery incident has an invalid shape")
    interval = payload["possible_loss_interval"]
    verification = payload["verification"]
    if (
        payload["database"] != name
        or any(
            not isinstance(payload[key], str) or not payload[key]
            for key in ("incident_id", "cause", "restored_snapshot_id")
        )
        or not isinstance(payload["acknowledged"], bool)
        or verification not in ("pending", "ok")
        or not isinstance(interval, dict)
        or set(interval) != {"start", "end"}
        or (payload["quarantine"] is not None and not isinstance(payload["quarantine"], str))
        or (verification == "pending" and payload["recovered_at"] is not None)
    ):
        raise DatabaseCorruptError(f"the {name} recovery incident has an invalid shape")
    instants = [payload["restored_snapshot_time"], interval["start"], interval["end"]]
    if verification == "ok":
        instants.append(payload["recovered_at"])
    if not all(_valid_instant(value) for value in instants):
        raise DatabaseCorruptError(f"the {name} recovery incident has an invalid timestamp")
    return payload


def read_incident(data_dir: Path, name: str) -> dict[str, Any] | None:
    """The recovery incident of one database, or ``None``; malformed is corrupt."""
    path = incident_path(data_dir, name)
    try:
        serialized = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeError as exc:
        raise DatabaseCorruptError(f"the {name} recovery incident is not valid UTF-8") from exc
    except OSError as exc:
        raise DatabaseUnavailableError(f"the {name} recovery incident could not be read") from exc
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise DatabaseCorruptError(f"the {name} recovery incident is not valid JSON") from exc
    return _parse_incident(payload, name)


def read_incidents(data_dir: Path) -> dict[str, dict[str, Any]]:
    """Every recorded recovery incident by database name."""
    root = Path(data_dir) / INCIDENT_ROOT_NAME
    try:
        entries = sorted(root.iterdir())
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise DatabaseUnavailableError("recovery incidents could not be listed") from exc
    incidents: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.suffix != ".json" or entry.name.startswith("."):
            continue
        try:
            validate_database_name(entry.stem)
        except ValueError:
            continue
        incident = read_incident(data_dir, entry.stem)
        if incident is not None:
            incidents[entry.stem] = incident
    return incidents


def active_incidents(data_dir: Path) -> list[dict[str, Any]]:
    """Unacknowledged incidents, oldest failure first."""
    return sorted(
        (
            incident
            for incident in read_incidents(data_dir).values()
            if not incident["acknowledged"]
        ),
        key=lambda incident: (incident["possible_loss_interval"]["end"], incident["database"]),
    )


def acknowledge_incident(data_dir: Path, incident_id: str) -> bool:
    """Acknowledge exactly the incident the caller observed.

    Returns ``False`` when no incident exists at all. Raises
    ``IncidentConflictError`` when incidents exist but none has this id, so a
    stale view never acknowledges a newer incident.
    """
    lock = acquire_operation_lock(data_dir)
    if lock is None:
        raise DatabaseUnavailableError("recovery incident acknowledgement lock is busy")
    try:
        incidents = read_incidents(data_dir)
        if not incidents:
            return False
        for name, incident in incidents.items():
            if incident["incident_id"] != incident_id:
                continue
            if not incident["acknowledged"]:
                _write_json_durably(
                    incident_path(data_dir, name),
                    {**incident, "acknowledged": True},
                    what="recovery incident acknowledgement",
                )
            return True
        raise IncidentConflictError("the recovery incident has changed; refresh status")
    finally:
        lock.release()


def pending_restore(data_dir: Path, name: str) -> bool:
    """Whether an interrupted restore of ``name`` must be resumed or confirmed."""
    incident = read_incident(data_dir, name)
    return incident is not None and incident["verification"] == "pending"


# ---------------------------------------------------------------------------
# Probing and compatibility
# ---------------------------------------------------------------------------


def _ledger_breaks(connection: sqlite3.Connection, spec: DatabaseSpec) -> list[str]:
    declared = {migration.name for migration in spec.migrations}
    return sorted(
        str(name)
        for name, breaks_older in connection.execute(
            "SELECT name, breaks_older FROM kernel_migrations"
        )
        if str(name) not in declared and breaks_older
    )


def _schema_compatible(connection: sqlite3.Connection, spec: DatabaseSpec) -> bool:
    """Whether opening would reconcile this file additively instead of refusing it."""
    try:
        schema_changes(
            connection,
            declared_schema(KERNEL_SCHEMA_SQL + spec.schema_sql),
            retired_indexes=spec.retired_indexes,
        )
    except DatabaseCorruptError:
        return False
    return True


def _member_compatible(path: Path, spec: DatabaseSpec | None) -> bool:
    """Reject a snapshot member this vBot could not open before anything changes."""
    if spec is None:
        return True
    try:
        with closing(sqlite3.connect(readonly_sqlite_uri(path), uri=True)) as connection:
            generation = int(connection.execute("PRAGMA user_version").fetchone()[0])
            return (
                generation == spec.format_generation
                and not _ledger_breaks(connection, spec)
                and _schema_compatible(connection, spec)
            )
    except sqlite3.Error as exc:
        translated = classified_error(exc, f"the {spec.name} snapshot member could not be read")
        if isinstance(translated, DatabaseUnavailableError):
            raise translated from exc
        return False
    except OSError as exc:
        raise DatabaseUnavailableError(
            f"the {spec.name} snapshot member could not be read"
        ) from exc


def _probe(spec: DatabaseSpec, expected_database_id: str) -> _Probe:
    """Decide whether the canonical file is usable, restorable, or must be left alone."""
    detected_at = utc_now()
    name = spec.name

    def damaged(cause: str) -> _Probe:
        return _Probe(False, True, cause, detected_at)

    def untouchable(cause: str) -> _Probe:
        return _Probe(False, False, cause, detected_at)

    if not spec.path.exists():
        return damaged(f"missing canonical {name} database")
    try:
        with closing(sqlite3.connect(readonly_sqlite_uri(spec.path), uri=True)) as connection:
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            generation = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if application_id != spec.application_id:
                return damaged(f"{name} database identity is invalid")
            if generation > spec.format_generation:
                return untouchable(f"{name} database is from a newer vBot")
            if generation != spec.format_generation:
                return damaged(f"{name} database format generation is invalid")
            identity = dict(connection.execute("SELECT key, value FROM kernel_meta").fetchall())
            database_id = identity.get("database_id")
            if (
                identity.get("database_name") != name
                or not valid_database_id(database_id)
                or database_id != expected_database_id
            ):
                return damaged(f"{name} database identity mismatch")
            if _ledger_breaks(connection, spec):
                return untouchable(f"{name} database was changed by a newer vBot")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return damaged(f"{name} database integrity failure")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                return damaged(f"{name} database foreign-key failure")
            if not _schema_compatible(connection, spec):
                return damaged(f"{name} database table schema mismatch")
        return _Probe(True, False, "", detected_at)
    except sqlite3.Error as exc:
        translated = classified_error(exc, "")
        if isinstance(translated, DatabaseUnavailableError):
            return untouchable(f"operational {name} database failure")
        return damaged(f"malformed canonical {name} database")
    except OSError:
        return untouchable(f"operational {name} database failure")


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def _install_member(
    data_dir: Path,
    name: str,
    target: Path,
    snapshot_dir: Path,
    member: SnapshotMember,
    spec: DatabaseSpec | None,
    *,
    before_replace: Any,
) -> Path | None:
    """Stage verified bytes, quarantine the old bundle, publish, verify again.

    Operational failures never select older data: after publication the
    restored database or the quarantined original can be the only good copy,
    so both are kept and the next open verifies them again.
    """
    source = member_path(snapshot_dir, member)
    if source is None:
        raise DatabaseCorruptError(f"snapshot member {name} is missing")
    temporary = target.with_name(f".{target.name}.restore.{uuid.uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, temporary)
        verify_member(snapshot_dir, member, spec=spec, path=temporary)
        fsync_file(temporary)
        quarantine_path = _new_quarantine_path(data_dir, name) if _bundle(target) else None
        before_replace(quarantine_path)
        quarantine = _quarantine_bundle(data_dir, name, target, destination=quarantine_path)
        if quarantine.had_bundle and not quarantine.succeeded:
            raise DatabaseUnavailableError(quarantine.reason or f"{name} quarantine failed")
        os.replace(temporary, target)
        fsync_dir(target.parent)
        verify_database_file(target, name=name, spec=spec, expected_database_id=member.database_id)
        return quarantine.path
    except (OSError, sqlite3.Error, DatabaseCorruptError) as exc:
        raise DatabaseUnavailableError(
            f"the {name} snapshot member could not be installed"
        ) from exc
    finally:
        with suppress(OSError):
            temporary.unlink()


def _restore_member_locked(
    data_dir: Path,
    name: str,
    snapshot_dir: Path,
    manifest: SnapshotManifest,
    member: SnapshotMember,
    spec: DatabaseSpec | None,
    *,
    cause: str,
    failure_detected_at: str,
) -> bool:
    """Restore one verified member under the operation lock, with its incident."""
    target = canonical_database_path(data_dir, name)
    if has_live_connection(target):
        return False
    source = member_path(snapshot_dir, member)
    if source is None or not _member_compatible(source, spec):
        return False
    try:
        verify_member(snapshot_dir, member, spec=spec)
    except DatabaseCorruptError:
        return False
    pending = read_incident(data_dir, name)
    if pending is not None and (
        pending["verification"] != "pending"
        or pending["restored_snapshot_id"] != manifest.snapshot_id
    ):
        pending = None
    incident_id = str(pending["incident_id"]) if pending else uuid.uuid4().hex
    retained_quarantine = pending["quarantine"] if pending else None
    if retained_quarantine and not _bundle(Path(retained_quarantine) / target.name):
        # A fully rolled-back attempt only reserved this path; it holds no evidence.
        retained_quarantine = None
    if pending is not None:
        failure_detected_at = pending["possible_loss_interval"]["end"]
        cause = pending["cause"]

    def publish_pending(quarantine_path: Path | None) -> None:
        write_incident(
            data_dir,
            name,
            cause=cause,
            quarantine_path=retained_quarantine or quarantine_path,
            restored_snapshot_id=manifest.snapshot_id,
            restored_snapshot_time=manifest.created_at,
            failure_detected_at=failure_detected_at,
            verification="pending",
            incident_id=incident_id,
        )

    quarantine_path = _install_member(
        data_dir, name, target, snapshot_dir, member, spec, before_replace=publish_pending
    )
    # A publication failure is operational, not evidence that this snapshot is
    # unusable: it propagates so auto-restore stops instead of restoring an
    # older candidate over the already verified database.
    write_incident(
        data_dir,
        name,
        cause=cause,
        quarantine_path=retained_quarantine or quarantine_path,
        restored_snapshot_id=manifest.snapshot_id,
        restored_snapshot_time=manifest.created_at,
        failure_detected_at=failure_detected_at,
        verification="ok",
        incident_id=incident_id,
    )
    return True


def _confirm_pending(data_dir: Path, spec: DatabaseSpec) -> None:
    """Complete an interrupted restore whose member was already installed."""
    pending = read_incident(data_dir, spec.name)
    if pending is None or pending["verification"] != "pending":
        return
    snapshot_dir = snapshot_root(data_dir) / pending["restored_snapshot_id"]
    manifest = read_manifest(data_dir, snapshot_dir)
    member = None if manifest is None else manifest.members.get(spec.name)
    if member is None:
        return
    try:
        verify_member(snapshot_dir, member, spec=spec, path=spec.path)
    except DatabaseCorruptError:
        # An interruption before quarantine left the original usable. It is
        # not evidence that the requested snapshot was restored.
        return
    write_incident(
        data_dir,
        spec.name,
        cause=pending["cause"],
        quarantine_path=pending["quarantine"],
        restored_snapshot_id=pending["restored_snapshot_id"],
        restored_snapshot_time=pending["restored_snapshot_time"],
        failure_detected_at=pending["possible_loss_interval"]["end"],
        verification="ok",
        incident_id=pending["incident_id"],
    )


def auto_restore_if_needed(data_dir: Path, spec: DatabaseSpec, expected_database_id: str) -> bool:
    """Restore ``spec`` from the newest verified member after a locked re-probe.

    Returns ``True`` only when a member was installed. A usable database is left
    alone (an interrupted restore that already installed its member is
    confirmed). Busy, locked or newer-vBot databases are never replaced.
    """
    data_dir = Path(data_dir)
    lock = acquire_operation_lock(data_dir)
    if lock is None:
        return False
    try:
        probe = _probe(spec, expected_database_id)
        if probe.usable:
            _confirm_pending(data_dir, spec)
            return False
        if not probe.recoverable:
            return False
        for snapshot_dir, manifest, member in member_restore_candidates(
            data_dir, spec.name, database_id=expected_database_id, spec=spec
        ):
            try:
                restored = _restore_member_locked(
                    data_dir,
                    spec.name,
                    snapshot_dir,
                    manifest,
                    member,
                    spec,
                    cause=probe.cause,
                    failure_detected_at=probe.detected_at,
                )
            except DatabaseUnavailableError:
                return False
            if restored:
                return True
        return False
    finally:
        lock.release()


def restore_data_snapshot(
    data_dir: Path,
    snapshot_dir: Path,
    *,
    specs: Iterable[DatabaseSpec] = (),
    names: Iterable[str] | None = None,
    cause: str = "manual operator restore",
    check_only: bool = False,
) -> list[str]:
    """Restore members of one verified data snapshot while Runtime is stopped.

    ``names`` selects members; by default every member is restored. Every
    selected member must verify and match the database registered in the
    marker before anything changes; ``check_only`` stops after that check.
    The maintenance guard covers the whole restore, so an interrupted restore
    keeps Runtime from starting until the restore is repeated. ``specs`` add
    compatibility and owner-fact checks for the members they describe.
    Returns the selected member names.
    """
    data_dir = Path(data_dir)
    marker = read_marker(data_dir)
    if marker is None:
        raise DatabaseFormatError(
            f"the data directory does not authorize a current-format data store: {data_dir}"
        )
    manifest = read_manifest(data_dir, snapshot_dir)
    if manifest is None:
        raise DatabaseCorruptError(f"snapshot is missing or malformed: {Path(snapshot_dir).name}")
    selected = sorted(manifest.members) if names is None else sorted(set(names))
    if not selected:
        raise ValueError("no snapshot member selected")
    known_specs = {spec.name: spec for spec in specs}
    for name in selected:
        member = manifest.members.get(name)
        if member is None:
            raise DatabaseFormatError(f"snapshot {manifest.snapshot_id} has no {name} member")
        entry = marker.databases.get(name)
        if entry is None or entry.database_id != member.database_id:
            raise DatabaseFormatError(
                f"snapshot member {name} does not belong to this data directory's {name} database"
            )
        verify_member(snapshot_dir, member, spec=known_specs.get(name))
        source = member_path(snapshot_dir, member)
        if source is None or not _member_compatible(source, known_specs.get(name)):
            raise DatabaseFormatError(f"snapshot member {name} cannot be opened by this vBot")
    if check_only:
        return selected
    restored: list[str] = []
    with maintenance(data_dir, RESTORE_OPERATION, resume=True):
        lock = acquire_operation_lock(data_dir)
        if lock is None:
            raise DatabaseUnavailableError("the data-store operation lock is busy")
        try:
            detected_at = utc_now()
            for name in selected:
                if not _restore_member_locked(
                    data_dir,
                    name,
                    snapshot_dir,
                    manifest,
                    manifest.members[name],
                    known_specs.get(name),
                    cause=cause,
                    failure_detected_at=detected_at,
                ):
                    raise DatabaseUnavailableError(
                        f"the {name} member could not be restored; it is open or changed"
                    )
                restored.append(name)
        finally:
            lock.release()
    return restored


__all__ = [
    "QuarantineResult",
    "acknowledge_incident",
    "active_incidents",
    "auto_restore_if_needed",
    "incident_path",
    "pending_restore",
    "quarantine_database",
    "quarantine_root",
    "read_incident",
    "read_incidents",
    "restore_data_snapshot",
    "write_incident",
]
