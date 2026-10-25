"""The previous installation and its retained data, owned by the update transaction."""

from __future__ import annotations

from pathlib import Path

from cli.application.packages import require_version_switch_release, validate_release
from cli.application.state import ApplicationError, Installation, Operation, operations
from core.database.snapshots import read_manifest, release_snapshot_retention, snapshot_root
from core.database.update_rollback import update_snapshot_reason


def previous_update(install: Installation) -> Operation:
    """Find the last activation, including a candidate whose normal start failed."""
    for operation in operations(install):
        if (
            (
                operation.phase == "completed"
                or (
                    operation.phase == "needs_attention"
                    and operation.candidate_version == install.version().name
                )
            )
            and operation.previous_version is not None
            and operation.candidate_version is not None
            and operation.previous_version != operation.candidate_version
        ):
            if operation.candidate_version != install.version().name:
                raise ApplicationError(
                    "The active version differs from the last recorded activation; "
                    "inspect it with vbot application status"
                )
            return operation
    raise ApplicationError("No previous installation is available for vbot rollback")


def require_rollback_target(
    install: Installation, *, source: str | None = None, restore_snapshot: str | None = None
) -> Operation:
    point = previous_update(install)
    if source is not None and point.id != source:
        raise ApplicationError("The previous installation changed; request vbot rollback again")
    manifest = validate_release(
        install.version(point.previous_version),
        shape=install.install_shape,
        remove_bytecode_caches=True,
        access=install.access,
    )
    require_version_switch_release(manifest)
    if restore_snapshot is not None:
        if not install.owns_server or restore_snapshot != point.data_snapshot:
            raise ApplicationError(
                "The selected snapshot is not the retained data of the previous installation"
            )
        data_dir = Path(install.server_data_directory or "")
        snapshot = read_manifest(data_dir, snapshot_root(data_dir) / restore_snapshot)
        if snapshot is None or snapshot.reason != update_snapshot_reason(point.id):
            raise ApplicationError("The previous installation's data snapshot is unavailable")
    return point


def release_unused_snapshot_claims(install: Installation) -> None:
    """Keep the last switch and uncertain operations; release only our own other claims."""
    if not install.owns_server:
        return
    records = operations(install)
    keep = {item.id for item in records if not item.terminal or item.phase == "needs_attention"}
    latest = next(
        (
            item
            for item in records
            if item.phase == "completed"
            and item.previous_version
            and item.candidate_version
            and item.previous_version != item.candidate_version
        ),
        None,
    )
    if latest is not None:
        keep.add(latest.id)
    data_dir = Path(install.server_data_directory or "")
    for item in records:
        if item.id not in keep:
            release_snapshot_retention(data_dir, item.id)
