"""Operational controls for the current-format SQLite Session store."""

from __future__ import annotations

import json
from pathlib import Path

from cli.rpc_client import rpc_call
from cli.server_management import CommandResult, ServerInstance, probe_health
from core.sessions.format import read_session_store_marker
from core.sessions.snapshots import (
    list_snapshots,
    restore_snapshot,
    snapshot_root,
    snapshot_summaries,
)


def session_store_status(instance: ServerInstance) -> CommandResult:
    """Show the operator-safe Session-store projection from the running server."""

    payload = rpc_call(instance, "session_store.status", {})
    if not payload.ok:
        return payload.to_command_result()
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
    health = probe_health(instance)
    if health.reachable:
        return CommandResult(
            ok=False,
            message=(
                f"refusing Session-store restore while the exact target is reachable at "
                f"{instance.host}:{instance.port}; stop it first"
            ),
            instance=instance,
            health=health,
        )
    marker = read_session_store_marker(instance.data_dir)
    if marker is None:
        return CommandResult(
            ok=False,
            message="cannot restore a Session snapshot without a current-format marker",
            instance=instance,
            health=health,
        )
    try:
        snapshot = _snapshot_path(instance, snapshot_id)
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance, health=health)
    if snapshot not in list_snapshots(
        instance.data_dir, expected_database_id=str(marker["database_id"])
    ):
        return CommandResult(
            ok=False,
            message=f"snapshot is missing or failed verification: {snapshot_id}",
            instance=instance,
            health=health,
        )
    restored = restore_snapshot(
        instance.data_dir,
        instance.data_dir / "sessions.db",
        snapshot,
    )
    return CommandResult(
        ok=restored,
        message=(
            f"restored Session snapshot {snapshot_id}"
            if restored
            else f"Session snapshot restore failed: {snapshot_id}"
        ),
        instance=instance,
        health=health,
    )


def _snapshot_path(instance: ServerInstance, snapshot_id: str) -> Path:
    if not snapshot_id or Path(snapshot_id).name != snapshot_id:
        raise ValueError("snapshot id must be a single directory name")
    return snapshot_root(instance.data_dir) / snapshot_id
