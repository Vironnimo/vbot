"""Operational controls for the current-format SQLite Session store."""

from __future__ import annotations

import json
from pathlib import Path

from cli.rpc_client import rpc_call
from cli.server_management import (
    DEFAULT_SERVICE_NAME,
    CommandResult,
    ServerInstance,
    is_systemd_managed,
    probe_health,
    start_server,
    start_systemd_server,
    stop_server,
    stop_systemd_server,
)
from core.sessions.errors import SessionStorageError
from core.sessions.format import read_session_store_marker
from core.sessions.snapshots import (
    list_snapshots,
    read_recovery_incident,
    read_snapshot_health,
    restore_snapshot_with_incident,
    snapshot_root,
    snapshot_summaries,
)
from core.sessions.store import SessionStore


def session_store_status(instance: ServerInstance) -> CommandResult:
    """Show the operator-safe Session-store projection from the running server."""

    payload = rpc_call(instance, "session_store.status", {})
    if not payload.ok:
        health = probe_health(instance)
        if health.reachable:
            return payload.to_command_result()
        projection = _offline_status_projection(instance)
        return CommandResult(
            ok=projection["state"] != "unrecoverable",
            message=json.dumps(projection, ensure_ascii=False, indent=2, sort_keys=True),
            instance=instance,
            health=health,
        )
    return CommandResult(
        ok=True,
        message=json.dumps(payload.data, ensure_ascii=False, indent=2, sort_keys=True),
        instance=instance,
    )


def session_store_snapshot_create(instance: ServerInstance, reason: str) -> CommandResult:
    """Request a verified snapshot from the running Runtime."""

    payload = rpc_call(instance, "session_store.snapshot_create", {"reason": reason})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=json.dumps(payload.data, ensure_ascii=False, indent=2, sort_keys=True),
        instance=instance,
    )


def session_store_incident_acknowledge(instance: ServerInstance, incident_id: str) -> CommandResult:
    """Acknowledge exactly one currently visible recovery incident."""

    payload = rpc_call(
        instance,
        "session_store.incident_acknowledge",
        {"incident_id": incident_id},
    )
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=json.dumps(payload.data, ensure_ascii=False, indent=2, sort_keys=True),
        instance=instance,
    )


def session_store_snapshot_list(instance: ServerInstance) -> CommandResult:
    """List verified snapshot summaries without opening a live Session store."""

    marker = read_session_store_marker(instance.data_dir)
    expected_id = None if marker is None else str(marker["database_id"])
    summaries = snapshot_summaries(instance.data_dir, expected_database_id=expected_id)
    return CommandResult(
        ok=True,
        message=json.dumps({"snapshots": summaries}, ensure_ascii=False, indent=2, sort_keys=True),
        instance=instance,
    )


def session_store_snapshot_verify(instance: ServerInstance, snapshot_id: str) -> CommandResult:
    """Verify that one fixed-root snapshot is complete and identity-matched."""

    try:
        snapshot = _snapshot_path(instance, snapshot_id)
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    marker = read_session_store_marker(instance.data_dir)
    expected_id = None if marker is None else str(marker["database_id"])
    verified = snapshot in list_snapshots(instance.data_dir, expected_database_id=expected_id)
    if not verified:
        return CommandResult(
            ok=False,
            message=f"snapshot is missing or failed verification: {snapshot_id}",
            instance=instance,
        )
    summary = next(
        item
        for item in snapshot_summaries(instance.data_dir, expected_database_id=expected_id)
        if item["snapshot_id"] == snapshot_id
    )
    return CommandResult(
        ok=True,
        message=json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        instance=instance,
    )


def session_store_snapshot_restore(
    instance: ServerInstance, snapshot_id: str, confirm: bool
) -> CommandResult:
    """Restore one verified snapshot only while the exact target is stopped."""

    if not confirm:
        return CommandResult(
            ok=False,
            message="refusing Session-store restore without confirmation; re-run with --yes",
            instance=instance,
        )
    marker = read_session_store_marker(instance.data_dir)
    if marker is None:
        return CommandResult(
            ok=False,
            message="cannot restore a Session snapshot without a current-format marker",
            instance=instance,
        )
    try:
        snapshot = _snapshot_path(instance, snapshot_id)
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    if snapshot not in list_snapshots(
        instance.data_dir, expected_database_id=str(marker["database_id"])
    ):
        return CommandResult(
            ok=False,
            message=f"snapshot is missing or failed verification: {snapshot_id}",
            instance=instance,
        )
    health = probe_health(instance)
    if health.reachable and not health.is_vbot:
        return CommandResult(
            ok=False,
            message=(
                f"refusing Session-store restore because a non-vBot process owns "
                f"{instance.host}:{instance.port}"
            ),
            instance=instance,
            health=health,
        )
    was_running = health.is_vbot
    systemd_managed = was_running and is_systemd_managed(instance, DEFAULT_SERVICE_NAME)
    if was_running:
        stopped = (
            stop_systemd_server(instance, DEFAULT_SERVICE_NAME)
            if systemd_managed
            else stop_server(instance)
        )
        if not stopped.ok or probe_health(instance).reachable:
            return CommandResult(
                ok=False,
                message=f"could not stop and verify the exact vBot target: {stopped.message}",
                instance=instance,
                health=stopped.health,
            )
    restored = restore_snapshot_with_incident(
        instance.data_dir,
        instance.data_dir / "sessions.db",
        snapshot,
        cause="manual operator restore",
    )
    if restored:
        store: SessionStore | None = None
        try:
            store = SessionStore(instance.data_dir / "sessions.db")
            store.verify_read_write()
        except (OSError, SessionStorageError) as exc:
            return CommandResult(
                ok=False,
                message=f"restored Session snapshot failed post-restore verification: {exc}",
                instance=instance,
                health=health,
            )
        finally:
            if store is not None:
                store.close()
    restarted: CommandResult | None = None
    if restored and was_running:
        restarted = (
            start_systemd_server(instance, DEFAULT_SERVICE_NAME)
            if systemd_managed
            else start_server(instance)
        )
        if not restarted.ok:
            return CommandResult(
                ok=False,
                message=(
                    f"restored Session snapshot {snapshot_id}, but restoring the prior server "
                    f"state failed: {restarted.message}"
                ),
                instance=instance,
                health=restarted.health,
            )
    return CommandResult(
        ok=restored,
        message=(
            f"restored Session snapshot {snapshot_id}"
            + (" and restarted the server" if restarted is not None else "")
            if restored
            else f"Session snapshot restore failed: {snapshot_id}"
        ),
        instance=instance,
        health=health,
    )


def _offline_status_projection(instance: ServerInstance) -> dict[str, object]:
    """Inspect a stopped current-format store even when Runtime cannot start."""

    marker: dict[str, object] | None = None
    try:
        marker = read_session_store_marker(instance.data_dir)
        if marker is None:
            raise SessionStorageError("current-format Session marker is missing")
        store = SessionStore(instance.data_dir / "sessions.db")
        try:
            return store.status_projection()
        finally:
            store.close()
    except (OSError, SessionStorageError) as exc:
        expected_id = None if marker is None else str(marker.get("database_id"))
        incident = read_recovery_incident(instance.data_dir)
        return {
            "state": "unrecoverable",
            "reason": f"{type(exc).__name__}: {exc}",
            "database_id": expected_id,
            "marker_state": None if marker is None else marker.get("state"),
            "schema_version": None if marker is None else marker.get("schema_version"),
            "fts": {"state": "unavailable", "reason": "canonical store did not open"},
            "snapshots": snapshot_summaries(instance.data_dir, expected_database_id=expected_id),
            "snapshot_health": read_snapshot_health(instance.data_dir),
            "incident": (
                incident if incident and not incident.get("acknowledged", False) else None
            ),
        }


def _snapshot_path(instance: ServerInstance, snapshot_id: str) -> Path:
    if not snapshot_id or Path(snapshot_id).name != snapshot_id:
        raise ValueError("snapshot id must be a single directory name")
    return snapshot_root(instance.data_dir) / snapshot_id
