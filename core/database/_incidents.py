"""Durable recovery incident documents, acknowledgement and restore identity."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from core.database._files import fsync_dir, fsync_file
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseUnavailableError,
    IncidentConflictError,
)
from core.database.marker import (
    acquire_operation_lock,
)
from core.database.spec import (
    validate_database_name,
)
from core.utils.timestamps import is_canonical_timestamp, utc_now_timestamp

INCIDENT_ROOT_NAME = "incidents"

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

_INTERVAL_KEYS = frozenset({"start", "end"})


def incident_path(data_dir: Path, name: str) -> Path:
    validate_database_name(name)
    return Path(data_dir) / INCIDENT_ROOT_NAME / f"{name}.json"


def _write_json_durably(path: Path, payload: Mapping[str, Any], *, what: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
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
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish the durable recovery incident of one database, or raise.

    ``previous`` is the incident this one continues (a pending restore that is
    resumed or confirmed); fields a newer vBot added to it are kept.
    """
    detected_at = failure_detected_at or utc_now_timestamp()
    previous_interval = (previous or {}).get("possible_loss_interval")
    payload: dict[str, Any] = {
        **_unknown_fields(previous, _INCIDENT_KEYS),
        "incident_id": incident_id or uuid.uuid4().hex,
        "database": name,
        "cause": cause,
        "quarantine": str(quarantine_path) if quarantine_path else None,
        "restored_snapshot_id": restored_snapshot_id,
        "restored_snapshot_time": restored_snapshot_time,
        "recovered_at": None
        if verification == "pending"
        else (recovered_at or utc_now_timestamp()),
        "verification": verification,
        "possible_loss_interval": {
            **_unknown_fields(previous_interval, _INTERVAL_KEYS),
            "start": restored_snapshot_time,
            "end": detected_at,
        },
        "acknowledged": False,
    }
    _write_json_durably(incident_path(data_dir, name), payload, what="recovery incident")
    return payload


def _unknown_fields(payload: object, known: frozenset[str]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    return {key: value for key, value in payload.items() if key not in known}


def _parse_incident(payload: object, name: str) -> dict[str, Any]:
    """Validate every known field; fields a newer vBot added are kept."""
    if not isinstance(payload, dict) or not set(payload) >= _INCIDENT_KEYS:
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
        or not set(interval) >= _INTERVAL_KEYS
        or (payload["quarantine"] is not None and not isinstance(payload["quarantine"], str))
        or (verification == "pending" and payload["recovered_at"] is not None)
    ):
        raise DatabaseCorruptError(f"the {name} recovery incident has an invalid shape")
    instants = [payload["restored_snapshot_time"], interval["start"], interval["end"]]
    if verification == "ok":
        instants.append(payload["recovered_at"])
    # An incident is written with canonical timestamps; any other form is damage.
    if not all(is_canonical_timestamp(value) for value in instants):
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


def latest_restore_id(data_dir: Path, name: str) -> str | None:
    """The id of the latest restore recorded for ``name``, or ``None`` when none was.

    Every restore (automatic, operator or update rollback) publishes a new
    incident before anything is replaced; resuming, completing or acknowledging
    it keeps the id, and incidents live outside data snapshots. The id
    therefore changes whenever a restore began, including one that stopped
    before the swap, and never otherwise. A malformed incident raises like
    ``read_incident``.
    """
    incident = read_incident(data_dir, name)
    return None if incident is None else str(incident["incident_id"])
