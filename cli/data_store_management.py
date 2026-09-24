"""Operational controls for the data directory's canonical SQLite databases."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from core.database import (
    DatabaseError,
    open_database,
    read_marker,
    read_verified_manifest,
    restore_data_snapshot,
    snapshot_root,
    snapshot_summaries,
    snapshot_summary,
)

if TYPE_CHECKING:
    from core.database import DatabaseSpec

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"})
_LOCAL_ONLY_MESSAGE = (
    "This data-store command reads the local data directory. Run it on the "
    "server machine with a loopback target and the matching --data-dir."
)
_FAILED_STATES = frozenset({"unavailable", "maintenance"})


def data_store_status(instance: ServerInstance) -> CommandResult:
    """Show data-store health from the running server, or read it locally when stopped."""
    payload = rpc_call(instance, "data_store.status", {})
    if payload.ok:
        return _json_result(instance, payload.data)
    health = probe_health(instance)
    if health.reachable or instance.host not in _LOOPBACK_HOSTS:
        return payload.to_command_result()
    projection = _local_status(instance.data_dir)
    projection["source"] = "local"
    projection["data_dir"] = instance.data_dir.as_posix()
    return CommandResult(
        ok=projection["state"] not in _FAILED_STATES,
        message=_dump(projection),
        instance=instance,
        health=health,
    )


def data_store_snapshot_create(instance: ServerInstance, reason: str) -> CommandResult:
    """Request a verified data snapshot of every canonical database from the server."""
    payload = rpc_call(instance, "data_store.snapshot_create", {"reason": reason})
    if not payload.ok:
        return payload.to_command_result()
    return _json_result(instance, payload.data)


def data_store_incident_acknowledge(instance: ServerInstance, incident_id: str) -> CommandResult:
    """Acknowledge exactly one currently visible recovery incident."""
    payload = rpc_call(instance, "data_store.incident_acknowledge", {"incident_id": incident_id})
    if not payload.ok:
        return payload.to_command_result()
    return _json_result(instance, payload.data)


def data_store_snapshot_list(instance: ServerInstance) -> CommandResult:
    """List verified data snapshots without opening any database."""
    if instance.host not in _LOOPBACK_HOSTS:
        return CommandResult(ok=False, message=_LOCAL_ONLY_MESSAGE, instance=instance)
    try:
        summaries = snapshot_summaries(
            instance.data_dir,
            specs=_specs_by_name(instance.data_dir),
            expected=_registered_ids(instance.data_dir),
        )
    except DatabaseError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    return _json_result(instance, {"snapshots": summaries})


def data_store_snapshot_verify(instance: ServerInstance, snapshot_id: str) -> CommandResult:
    """Verify every member of one data snapshot against this data directory."""
    if instance.host not in _LOOPBACK_HOSTS:
        return CommandResult(ok=False, message=_LOCAL_ONLY_MESSAGE, instance=instance)
    try:
        snapshot = _snapshot_path(instance, snapshot_id)
        manifest = read_verified_manifest(
            instance.data_dir,
            snapshot,
            specs=_specs_by_name(instance.data_dir),
            expected=_registered_ids(instance.data_dir),
        )
    except (ValueError, DatabaseError) as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    if manifest is None:
        return CommandResult(
            ok=False,
            message=f"snapshot is missing or failed verification: {snapshot_id}",
            instance=instance,
        )
    return _json_result(instance, snapshot_summary(manifest))


def data_store_snapshot_restore(
    instance: ServerInstance,
    snapshot_id: str,
    confirm: bool,
    databases: Iterable[str] = (),
) -> CommandResult:
    """Restore members of one verified data snapshot while the exact target is stopped.

    Every member is restored unless ``databases`` names some. A server that
    was running is stopped first and started again afterwards.
    """
    if instance.host not in _LOOPBACK_HOSTS:
        return CommandResult(ok=False, message=_LOCAL_ONLY_MESSAGE, instance=instance)
    if not confirm:
        return CommandResult(
            ok=False,
            message="refusing data-store restore without confirmation; re-run with --yes",
            instance=instance,
        )
    names = sorted(set(databases)) or None
    specs = _specs_by_name(instance.data_dir)
    try:
        snapshot = _snapshot_path(instance, snapshot_id)
        # Check before stopping the server: nothing changes yet.
        restore_data_snapshot(
            instance.data_dir, snapshot, specs=specs.values(), names=names, check_only=True
        )
    except (OSError, ValueError, DatabaseError) as exc:
        return CommandResult(
            ok=False,
            message=f"snapshot cannot be restored: {snapshot_id}: {exc}",
            instance=instance,
        )
    health = probe_health(instance)
    if health.reachable and not health.is_vbot:
        return CommandResult(
            ok=False,
            message=(
                "refusing data-store restore because a non-vBot process owns "
                f"{instance.host}:{instance.port}"
            ),
            instance=instance,
            health=health,
        )
    was_running = health.is_vbot
    systemd_managed = is_systemd_managed(instance, DEFAULT_SERVICE_NAME)
    # A closed listener can still belong to a Runtime draining its databases.
    # The lifecycle owner also waits for that exact control-record process.
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
    try:
        restored = restore_data_snapshot(
            instance.data_dir, snapshot, specs=specs.values(), names=names
        )
        _verify_restored(restored, specs)
    except (OSError, ValueError, DatabaseError) as exc:
        return CommandResult(
            ok=False,
            message=f"data snapshot restore failed: {snapshot_id}: {exc}",
            instance=instance,
            health=health,
        )
    restarted: CommandResult | None = None
    if was_running:
        restarted = (
            start_systemd_server(instance, DEFAULT_SERVICE_NAME)
            if systemd_managed
            else start_server(instance)
        )
        if not restarted.ok:
            return CommandResult(
                ok=False,
                message=(
                    f"restored data snapshot {snapshot_id}, but restoring the prior server "
                    f"state failed: {restarted.message}"
                ),
                instance=instance,
                health=restarted.health,
            )
    return CommandResult(
        ok=True,
        message=(
            f"restored data snapshot {snapshot_id} ({', '.join(restored)})"
            + (" and restarted the server" if restarted is not None else "")
        ),
        instance=instance,
        health=health,
    )


def _verify_restored(restored: Iterable[str], specs: dict[str, DatabaseSpec]) -> None:
    """Open each restored database this vBot declares and exercise its read/write path."""
    for name in restored:
        spec = specs.get(name)
        if spec is None:
            continue
        database = open_database(spec)
        try:
            database.verify_read_write()
        finally:
            database.close()


def _local_status(data_dir: Path) -> dict[str, Any]:
    from core.database import data_store_status as read_status

    return read_status(data_dir, specs=_specs_by_name(data_dir).values())


def _specs_by_name(data_dir: Path) -> dict[str, DatabaseSpec]:
    from core.runtime.databases import canonical_database_specs

    return {spec.name: spec for spec in canonical_database_specs(data_dir)}


def _registered_ids(data_dir: Path) -> dict[str, str] | None:
    marker = read_marker(data_dir)
    if marker is None:
        return None
    return {name: entry.database_id for name, entry in marker.databases.items()}


def _snapshot_path(instance: ServerInstance, snapshot_id: str) -> Path:
    if not snapshot_id or Path(snapshot_id).name != snapshot_id:
        raise ValueError("snapshot id must be a single directory name")
    return snapshot_root(instance.data_dir) / snapshot_id


def _json_result(instance: ServerInstance, data: Any) -> CommandResult:
    return CommandResult(ok=True, message=_dump(data), instance=instance)


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
