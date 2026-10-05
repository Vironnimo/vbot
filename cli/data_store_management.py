"""Operational controls for the data directory's canonical databases and configuration backups."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cli.rpc_client import rpc_call
from cli.server_management import (
    CommandResult,
    ServerInstance,
    classify_server,
    probe_health,
    probe_health_patiently,
    start_server,
    stop_server,
)
from core.database import (
    ConfigRestore,
    DatabaseError,
    SnapshotRestore,
    UnregisteredDatabase,
    describe_config_backup,
    list_config_backups,
    open_database,
    read_marker,
    read_verified_manifest,
    restore_config_backup,
    restore_data_snapshot,
    snapshot_root,
    snapshot_summaries,
    snapshot_summary,
    unregister_database,
)
from core.utils.server_control import live_server_ports

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


def data_store_unregister(instance: ServerInstance, name: str, confirm: bool) -> CommandResult:
    """Release registered Extension database ``name`` whose Extension was removed.

    Its files move to quarantine and its registration is dropped, so data
    snapshots no longer wait for it; earlier snapshots keep their copy. The
    running server performs it and refuses while an Extension has the database
    open. When a loopback target is stopped and no server runs on the data
    directory, the local data directory is changed directly.
    """
    if not confirm:
        return CommandResult(
            ok=False,
            message="refusing to unregister a database without confirmation; re-run with --yes",
            instance=instance,
        )
    payload = rpc_call(instance, "data_store.unregister", {"name": name})
    if payload.ok:
        return _json_result(instance, payload.data)
    health = probe_health(instance)
    if health.reachable or instance.host not in _LOOPBACK_HOSTS:
        return payload.to_command_result()
    try:
        ports = live_server_ports(instance.data_dir)
    except OSError as exc:
        return CommandResult(
            ok=False,
            message=f"the servers running on the data directory could not be checked ({exc})",
            instance=instance,
            health=health,
        )
    if ports:
        return CommandResult(
            ok=False,
            message=(
                "a vBot server is running on the data directory (port "
                + ", ".join(str(port) for port in ports)
                + "); run the command against that server"
            ),
            instance=instance,
            health=health,
        )
    try:
        released = unregister_database(instance.data_dir, name)
    except (OSError, ValueError, DatabaseError) as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance, health=health)
    return CommandResult(
        ok=True,
        message=_dump({"unregistered": _released_projection(released), "source": "local"}),
        instance=instance,
        health=health,
    )


def _released_projection(released: UnregisteredDatabase) -> dict[str, Any]:
    return {
        "name": released.name,
        "database_id": released.database_id,
        "quarantine": None if released.quarantine is None else released.quarantine.as_posix(),
    }


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
    """Verify every database member and JSON document of one data snapshot."""
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
    *,
    documents: bool = False,
    complete: bool = False,
) -> CommandResult:
    """Restore parts of one verified data snapshot while the exact target is stopped.

    Every database is restored unless ``databases`` names some or only
    ``documents`` is selected. ``documents`` restores the JSON document set as
    one unit. ``complete`` restores every database and the documents and
    moves databases registered after the snapshot to quarantine. A server that
    was running, busy or answering, is stopped first and started again afterwards.
    """
    if instance.host not in _LOOPBACK_HOSTS:
        return CommandResult(ok=False, message=_LOCAL_ONLY_MESSAGE, instance=instance)
    selected_databases = sorted(set(databases))
    if complete and (selected_databases or documents):
        return CommandResult(
            ok=False,
            message="--all restores the complete snapshot; do not combine it with "
            "--database or --documents",
            instance=instance,
        )
    if not confirm:
        return CommandResult(
            ok=False,
            message="refusing data-store restore without confirmation; re-run with --yes",
            instance=instance,
        )
    # No selector restores every database; --documents alone restores no database.
    names = selected_databases if selected_databases or documents else None
    specs = _specs_by_name(instance.data_dir)

    def restore(snapshot: Path, *, check_only: bool) -> SnapshotRestore:
        return restore_data_snapshot(
            instance.data_dir,
            snapshot,
            specs=specs.values(),
            names=names,
            documents=documents or complete,
            retire_unlisted=complete,
            check_only=check_only,
        )

    def restore_and_verify(snapshot: Path) -> SnapshotRestore:
        restored = restore(snapshot, check_only=False)
        _verify_restored(restored.databases, specs)
        return restored

    try:
        snapshot = _snapshot_path(instance, snapshot_id)
        # Check before stopping the server: nothing changes yet.
        restore(snapshot, check_only=True)
    except (OSError, ValueError, DatabaseError) as exc:
        return CommandResult(
            ok=False,
            message=f"snapshot cannot be restored: {snapshot_id}: {exc}",
            instance=instance,
        )
    return _restore_while_stopped(
        instance,
        lambda: _describe_restore(restore_and_verify(snapshot)),
        noun="data snapshot",
        item_id=snapshot_id,
    )


def data_store_config_backup_list(instance: ServerInstance) -> CommandResult:
    """List the configuration backups, newest first, from the local data directory."""
    if instance.host not in _LOOPBACK_HOSTS:
        return CommandResult(ok=False, message=_LOCAL_ONLY_MESSAGE, instance=instance)
    try:
        backups = list_config_backups(instance.data_dir)
    except DatabaseError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    items = []
    for index, backup in enumerate(backups):
        older = backups[index + 1].files if index + 1 < len(backups) else {}
        item = backup.summary()
        item["changed"] = sorted(
            path
            for path in backup.files.keys() | older.keys()
            if backup.files.get(path) != older.get(path)
        )
        items.append(item)
    return _json_result(instance, {"config_backups": items})


def data_store_config_backup_show(instance: ServerInstance, backup_id: str) -> CommandResult:
    """Show one configuration backup's files against the current ones."""
    if instance.host not in _LOOPBACK_HOSTS:
        return CommandResult(ok=False, message=_LOCAL_ONLY_MESSAGE, instance=instance)
    try:
        described = describe_config_backup(instance.data_dir, backup_id)
    except (ValueError, DatabaseError) as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    return _json_result(instance, described)


def data_store_config_backup_restore(
    instance: ServerInstance,
    backup_id: str,
    confirm: bool,
    files: Iterable[str] = (),
    *,
    complete: bool = False,
) -> CommandResult:
    """Restore files of one configuration backup while the exact target is stopped.

    ``files`` names the files to restore; ``complete`` restores every file of the
    backup. Files created after the backup and files whose folder no longer exists
    stay as they are. The state before the restore is backed up first.
    """
    if instance.host not in _LOOPBACK_HOSTS:
        return CommandResult(ok=False, message=_LOCAL_ONLY_MESSAGE, instance=instance)
    selected = sorted(set(files))
    if complete == bool(selected):
        return CommandResult(
            ok=False,
            message="name the files to restore with --file, or restore every file with --all",
            instance=instance,
        )
    if not confirm:
        return CommandResult(
            ok=False,
            message="refusing data-store restore without confirmation; re-run with --yes",
            instance=instance,
        )
    paths = None if complete else selected

    def restore(*, check_only: bool) -> ConfigRestore:
        return restore_config_backup(
            instance.data_dir, backup_id, paths=paths, check_only=check_only
        )

    try:
        # Check before stopping the server: nothing changes yet.
        plan = restore(check_only=True)
    except (OSError, ValueError, DatabaseError) as exc:
        return CommandResult(
            ok=False,
            message=f"configuration backup cannot be restored: {backup_id}: {exc}",
            instance=instance,
        )
    # An interrupted restore of this backup still has to finish, even with every file equal.
    if not plan.restored and not plan.interrupted:
        return CommandResult(
            ok=True,
            message=f"nothing to restore from configuration backup {backup_id} ("
            + _describe_config_restore(plan)
            + ")",
            instance=instance,
        )
    return _restore_while_stopped(
        instance,
        lambda: _describe_config_restore(restore(check_only=False)),
        noun="configuration backup",
        item_id=backup_id,
    )


def _describe_config_restore(restored: ConfigRestore) -> str:
    parts = [f"{len(restored.restored)} restored: " + ", ".join(restored.restored)]
    if restored.unchanged:
        parts.append(f"{len(restored.unchanged)} already equal")
    parts.extend(f"{path} left alone: {reason}" for path, reason in restored.skipped.items())
    if restored.created_after:
        parts.append("created after the backup and kept: " + ", ".join(restored.created_after))
    if restored.interrupted:
        parts.append("the interrupted restore of this backup is finished")
    if restored.before_restore is not None:
        parts.append(
            f"the replaced state is configuration backup {restored.before_restore}; "
            "restore it to take this restore back"
        )
    return "; ".join(parts)


def _restore_while_stopped(
    instance: ServerInstance, restore: Callable[[], str], *, noun: str, item_id: str
) -> CommandResult:
    """Run ``restore`` while the exact target is stopped; start it again if it ran.

    ``restore`` changes the data directory and describes what it changed. A
    server that was running, busy or answering, is stopped first and started
    again only after the restore succeeded.
    """
    health = probe_health_patiently(instance)
    state = classify_server(instance, health=health)
    if state == "foreign":
        return CommandResult(
            ok=False,
            message=(
                "refusing data-store restore because another process or a vBot server of "
                f"another data directory holds {instance.host}:{instance.port}"
            ),
            instance=instance,
            health=health,
        )
    # A busy server runs as well: it is stopped like an answering one and started
    # again afterwards, never left stopped as if it had not been running.
    was_running = state != "absent"
    stop, start = _lifecycle(instance)
    # A closed listener can still belong to a Runtime draining its databases.
    # The lifecycle owner also waits for that exact control-record process.
    stopped = stop()
    if not stopped.ok or classify_server(instance) != "absent":
        return CommandResult(
            ok=False,
            message=f"could not stop and verify the exact vBot target: {stopped.message}",
            instance=instance,
            health=stopped.health,
        )
    try:
        described = restore()
    except (OSError, ValueError, DatabaseError) as exc:
        return CommandResult(
            ok=False,
            message=f"{noun} restore failed: {item_id}: {exc}",
            instance=instance,
            health=health,
        )
    restarted: CommandResult | None = None
    if was_running:
        restarted = start()
        if not restarted.ok:
            return CommandResult(
                ok=False,
                message=(
                    f"restored {noun} {item_id}, but restoring the prior server "
                    f"state failed: {restarted.message}"
                ),
                instance=instance,
                health=restarted.health,
            )
    return CommandResult(
        ok=True,
        message=(
            f"restored {noun} {item_id} ({described})"
            + (" and restarted the server" if restarted is not None else "")
        ),
        instance=instance,
        health=health,
    )


def _lifecycle(
    instance: ServerInstance,
) -> tuple[Callable[[], CommandResult], Callable[[], CommandResult]]:
    """Stop and start *instance* the way its owner does."""
    from cli.application import processes
    from cli.application.state import exclusive

    install = processes.owning_installation(instance)
    if install is None:
        return (lambda: stop_server(instance)), (lambda: start_server(instance))

    def stop() -> CommandResult:
        with exclusive(install.root):
            return processes.stop(install)

    def start() -> CommandResult:
        with exclusive(install.root):
            return processes.start(install)

    return stop, start


def _describe_restore(restored: SnapshotRestore) -> str:
    parts = list(restored.databases)
    if restored.documents is not None:
        documents = restored.documents
        described = (
            f"JSON documents: {len(documents.restored)} restored, {len(documents.removed)} removed"
            if documents.changed
            else "JSON documents: unchanged"
        )
        if documents.quarantine is not None:
            described += f"; replaced documents kept at {documents.quarantine}"
        parts.append(described)
    if restored.registered:
        parts.append("registered again: " + ", ".join(restored.registered))
    if restored.retired:
        parts.append("moved to quarantine: " + ", ".join(restored.retired))
    return "; ".join(parts)


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
