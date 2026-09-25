"""Operator-safe data-store status: databases, snapshots, incidents and maintenance."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.database._connections import classified_error, readonly_sqlite_uri
from core.database.errors import DatabaseError, DatabaseUnavailableError
from core.database.marker import MarkerEntry, read_maintenance, read_marker
from core.database.recovery import active_incidents
from core.database.snapshots import (
    missing_database_reason,
    read_snapshot_health,
    snapshot_inventory,
)
from core.database.spec import DatabaseHealth, DatabaseSpec, canonical_database_path

if TYPE_CHECKING:
    from core.database.database import Database

#: Overall states, most severe first.
STATUS_STATES = (
    "maintenance",
    "unavailable",
    "recovered_with_incident",
    "degraded",
    "snapshot_degraded",
    "healthy",
)


def data_store_status(
    data_dir: Path,
    *,
    databases: Iterable[Database] = (),
    specs: Iterable[DatabaseSpec] = (),
) -> dict[str, Any]:
    """Summarize the data directory's canonical databases for operators.

    ``databases`` are handles open in this process; their owner health is read
    through them. Every other registered database is checked read-only from its
    file, with owner health when ``specs`` describes it. Nothing is created,
    evolved or restored.
    """
    data_dir = Path(data_dir)
    problems: list[str] = []
    try:
        guard = read_maintenance(data_dir)
    except DatabaseError as exc:
        guard = None
        problems.append(str(exc))
    try:
        marker = read_marker(data_dir)
    except DatabaseError as exc:
        marker = None
        problems.append(str(exc))
    if marker is None and not problems:
        problems.append("the data directory has no data-store marker")
    entries = {} if marker is None else dict(marker.databases)
    open_databases = {database.name: database for database in databases}
    known_specs = {spec.name: spec for spec in specs}
    members: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(entries.items()):
        handle = open_databases.get(name)
        member_state: str
        if handle is not None and not handle.is_closed():
            health = handle.health()
            member_state, member_reason = health.state, health.reason
            details = dict(health.details)
        else:
            member_state, member_reason, details = _file_state(
                data_dir, name, entry, known_specs.get(name)
            )
        members[name] = {
            "database_id": entry.database_id,
            "format_generation": entry.format_generation,
            "state": member_state,
            "reason": member_reason,
            "details": details,
        }
    expected = {name: entry.database_id for name, entry in entries.items()}
    snapshots = snapshot_inventory(data_dir, expected=expected)
    snapshot_health = read_snapshot_health(data_dir)
    try:
        incidents = active_incidents(data_dir)
    except DatabaseError as exc:
        incidents = []
        problems.append(str(exc))
    unavailable = [
        f"{name}: {member['reason']}"
        for name, member in members.items()
        if member["state"] == "unavailable"
    ]
    degraded = [
        f"{name}: {member['reason']}"
        for name, member in members.items()
        if member["state"] == "degraded"
    ]
    state: str
    reason: str | None
    if guard is not None:
        state, reason = "maintenance", f"data maintenance is incomplete ({guard.operation})"
    elif problems or unavailable:
        state, reason = "unavailable", "; ".join(problems + unavailable)
    elif incidents:
        state, reason = "recovered_with_incident", None
    elif degraded:
        state, reason = "degraded", "; ".join(degraded)
    elif not snapshots or snapshot_health.get("state") != "healthy":
        state = "snapshot_degraded"
        reason = str(snapshot_health.get("reason") or "no verified data snapshot is available")
    else:
        state, reason = "healthy", None
    return {
        "state": state,
        "reason": reason,
        "maintenance": None
        if guard is None
        else {
            "operation": guard.operation,
            "started_at": guard.started_at or None,
            "pid": guard.pid or None,
        },
        "databases": members,
        "snapshots": snapshots,
        "snapshot_health": snapshot_health,
        "incidents": incidents,
    }


def _file_state(
    data_dir: Path, name: str, entry: MarkerEntry, spec: DatabaseSpec | None
) -> tuple[str, str | None, dict[str, Any]]:
    """Check a registered database that is not open here, without changing it."""
    path = canonical_database_path(data_dir, name)
    if not path.is_file():
        return "unavailable", missing_database_reason(name), {}
    try:
        with closing(sqlite3.connect(readonly_sqlite_uri(path), uri=True)) as connection:
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            generation = int(connection.execute("PRAGMA user_version").fetchone()[0])
            identity = dict(connection.execute("SELECT key, value FROM kernel_meta").fetchall())
            if (
                (spec is not None and application_id != spec.application_id)
                or identity.get("database_name") != name
                or identity.get("database_id") != entry.database_id
            ):
                return "unavailable", "the database file has another identity", {}
            if generation != entry.format_generation:
                return "unavailable", f"the database file is format generation {generation}", {}
            if spec is None or spec.health is None:
                return "healthy", None, {}
            health: DatabaseHealth = spec.health(connection)
            return health.state, health.reason, dict(health.details)
    except (sqlite3.Error, OSError) as exc:
        translated = classified_error(exc, "") if isinstance(exc, sqlite3.Error) else None
        if isinstance(translated, DatabaseUnavailableError) or isinstance(exc, OSError):
            return "unavailable", f"the database file cannot be read: {exc}", {}
        return "unavailable", f"the database file is damaged: {exc}", {}
