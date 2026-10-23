"""Restoring a configuration backup while no server runs on the data directory.

A restore replaces the selected files of one backup (``core.database.config_backups``):
never one damaged in the backup, never one whose folder no longer exists, never one
a backup cannot read because it or a folder on its path is a link. It leaves files
created after the backup alone and first backs up the current state, so a restore
can be taken back by restoring that backup. While it writes, it holds the
maintenance guard: an interrupted restore keeps Runtime from starting until it is
repeated, or until a restore of every file of another backup replaces it.

A restore prepared while the server still runs reserves its backup first
(:func:`reserve_config_backup`), because the server backs up once more when it
stops, and the retention after that backup could remove the backup about to be
restored. The reservation files, and how retention honours them, belong to
``config_backups`` with everything else under ``config-backups/``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# A module import: ``core.json_documents`` imports the kernel's snapshot freeze in turn.
import core.json_documents as json_documents
from core.database.config_backups import (
    ConfigBackup,
    ConfigBackupReservation,
    ConfigFile,
    _capture_config_backup_locked,
    _current_hashes,
    _data_path,
    _object_path,
    _write_reservation,
    config_backup_root,
    list_config_backups,
    read_config_backup,
)
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseFormatError,
    DatabaseUnavailableError,
)
from core.database.marker import (
    maintenance_locked,
    operation_lock,
    read_maintenance,
    require_maintenance_scope,
    require_no_maintenance,
)
from core.utils.atomic import atomic_write_bytes
from core.utils.file_status import is_link_status, stat_or_none

_LOGGER = logging.getLogger("vbot.database")

#: The maintenance operation a configuration restore holds, followed by the backup id.
CONFIG_RESTORE_OPERATION = "config restore"
# ``core.json_documents.DOCUMENTS_KEPT_BY_RESTORES``: restored only when named.
_KEPT_BY_FULL_RESTORE = (
    "restoring every file keeps the Secret Store, which holds the credentials and OAuth "
    "tokens changed since the backup; name this file to restore it"
)


@dataclass(frozen=True)
class ConfigRestore:
    """What a configuration restore changed, or would change for ``check_only``.

    ``skipped`` maps a file of the backup that was left alone to the reason;
    ``created_after`` lists current configuration files the backup does not
    hold, which a restore never removes. ``before_restore`` is the backup that
    holds the state before the restore (``None`` for a plan or when nothing
    changed). ``interrupted`` says that an earlier restore of this backup did
    not finish; the restore finishes it even when no file differs any more.
    ``replaced`` names the backup whose unfinished restore this restore of every
    file replaces, also when no file differs.
    """

    backup_id: str
    restored: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    skipped: Mapping[str, str] = field(default_factory=dict)
    created_after: tuple[str, ...] = ()
    before_restore: str | None = None
    interrupted: bool = False
    replaced: str | None = None


def reserve_config_backup(
    data_dir: Path, backup_id: str, *, now: datetime | None = None
) -> ConfigBackupReservation:
    """Keep ``backup_id`` from retention until the reservation is released or expires.

    For a restore checked while a server still runs on ``data_dir``: the server
    backs up once more when it stops, and the retention after that backup could
    remove the backup about to be restored. Works during data maintenance, so an
    interrupted restore can be repeated. Raises ``ValueError`` when there is no
    such backup, ``DatabaseCorruptError`` when its manifest is unreadable,
    ``DatabaseFormatError`` instead of either while its restore is unfinished, and
    ``DatabaseUnavailableError`` while the operation lock stays busy.
    """
    data_dir = Path(data_dir)
    moment = datetime.now(UTC) if now is None else now
    with operation_lock(data_dir):
        _read_restorable(data_dir, backup_id)
        return _write_reservation(config_backup_root(data_dir), backup_id, moment)


def restore_config_backup(
    data_dir: Path,
    backup_id: str,
    *,
    paths: Iterable[str] | None = None,
    check_only: bool = False,
    now: datetime | None = None,
) -> ConfigRestore:
    """Restore files of one configuration backup while no server runs on ``data_dir``.

    ``paths`` names the files to restore (``None``: every file of the backup
    except the Secret Store, which is restored only when named).
    A named file that is not in the backup, is damaged there, whose folder
    no longer exists (a deleted or renamed Agent, Project or Channel), or that
    is a link or lies below a linked folder (symbolic link or junction, which
    backups never read) refuses the restore with ``ValueError``; restoring every
    file skips such files instead. Every object is verified before anything
    changes; a missing or altered one raises ``DatabaseCorruptError``. Files
    created after the backup stay. Before the first change the current state is
    backed up and the maintenance guard held, so an interrupted restore keeps
    Runtime from starting until it is repeated or a restore of every file of
    another backup replaces it; while it is incomplete, any other restore raises
    ``DatabaseFormatError``, as does repeating it once its backup cannot be read.
    """
    data_dir = Path(data_dir)
    with operation_lock(data_dir):
        return _restore_config_backup_locked(
            data_dir, backup_id, paths=paths, check_only=check_only, now=now
        )


def _restore_config_backup_locked(
    data_dir: Path,
    backup_id: str,
    *,
    paths: Iterable[str] | None,
    check_only: bool,
    now: datetime | None,
) -> ConfigRestore:
    backup = _read_restorable(data_dir, backup_id)
    operation = _restore_operation(backup_id)
    explicit = paths is not None
    selected = (
        sorted(backup.files)
        if paths is None
        else sorted({path.replace("\\", "/") for path in paths})
    )
    if explicit and not selected:
        raise ValueError("name at least one file to restore")
    scope = {"files": selected, "all": not explicit}
    interrupted, replaced = _pending_restore(data_dir, operation, scope, complete=not explicit)
    current = _current_hashes(data_dir)
    root = config_backup_root(data_dir)
    restored: dict[str, tuple[bytes, ConfigFile]] = {}
    unchanged: list[str] = []
    skipped: dict[str, str] = {}
    for path in selected:
        item = backup.files.get(path)
        if item is None:
            raise ValueError(f"{path} is not in configuration backup {backup_id}")
        problem = _restore_problem(data_dir, item)
        if (
            problem is None
            and not explicit
            and current.get(path) != item.sha256
            and json_documents.is_kept_by_restores(path)
        ):
            problem = _KEPT_BY_FULL_RESTORE
        if problem is not None:
            if explicit:
                raise ValueError(f"{path} cannot be restored: {problem}")
            skipped[path] = problem
        elif current.get(path) == item.sha256:
            unchanged.append(path)
        else:
            restored[path] = (_verified_object(root, backup_id, item), item)
    plan = ConfigRestore(
        backup_id=backup_id,
        restored=tuple(restored),
        unchanged=tuple(unchanged),
        skipped=skipped,
        created_after=tuple(sorted(path for path in current if path not in backup.files)),
        interrupted=interrupted,
        replaced=replaced,
    )
    if check_only or not (restored or interrupted or replaced):
        return plan
    # A replaced restore left a half-restored state: it is backed up like any other.
    before = _before_restore_backup(data_dir, backup_id, interrupted=interrupted, now=now)
    # An interrupted attempt may have written every file already: entering and
    # leaving the guard finishes it, and taking over a replaced restore's guard ends it.
    with maintenance_locked(
        data_dir,
        operation,
        resume=True,
        replaces=None if replaced is None else _restore_operation(replaced),
        scope=scope,
    ):
        for path, (content, item) in restored.items():
            atomic_write_bytes(
                _data_path(data_dir, path),
                content,
                # A read-only file could not be written through its own bits.
                mode=item.mode if os.name == "posix" and item.mode & stat.S_IWUSR else None,
            )
    _LOGGER.info(
        "Restored configuration backup (backup=%s files=%d before_restore=%s replaced=%s)",
        backup_id,
        len(restored),
        before,
        replaced,
    )
    return ConfigRestore(
        backup_id=backup_id,
        restored=plan.restored,
        unchanged=plan.unchanged,
        skipped=skipped,
        created_after=plan.created_after,
        before_restore=before,
        interrupted=interrupted,
        replaced=replaced,
    )


def _restore_operation(backup_id: str) -> str:
    return f"{CONFIG_RESTORE_OPERATION} {backup_id}"


def _read_restorable(data_dir: Path, backup_id: str) -> ConfigBackup:
    """Read the backup a restore restores; explain the way out when its restore is unfinished.

    A guarded restore whose backup is gone or unreadable cannot be repeated, so
    the refusal names the other way to end its guard.
    """
    try:
        return read_config_backup(data_dir, backup_id)
    except (ValueError, DatabaseCorruptError) as exc:
        guard = read_maintenance(data_dir)
        if guard is None or guard.operation != _restore_operation(backup_id):
            raise
        raise DatabaseFormatError(
            f"{exc}, so its unfinished restore cannot be repeated; replace that restore by "
            "restoring every file of another backup (--all)"
        ) from exc


def _pending_restore(
    data_dir: Path, operation: str, scope: Mapping[str, object], *, complete: bool
) -> tuple[bool, str | None]:
    """Whether a restore of this backup is incomplete, and which unfinished one it replaces.

    Only a restore of every file (``complete``) replaces the unfinished restore of
    another backup: it leaves every file that backup holds in one known state.
    Any other incomplete operation is refused.
    """
    guard = read_maintenance(data_dir)
    if guard is None:
        return False, None
    if guard.operation == operation:
        if guard.scope is None:
            raise DatabaseFormatError(
                f"the interrupted restore of configuration backup "
                f"{operation.removeprefix(f'{CONFIG_RESTORE_OPERATION} ')} has no recorded "
                "file selection. Nothing was changed. Replace it by restoring every file "
                "of another backup (--all)"
            )
        require_maintenance_scope(data_dir, operation, scope)
        return True, None
    pending = guard.operation.removeprefix(f"{CONFIG_RESTORE_OPERATION} ")
    if pending == guard.operation:
        require_no_maintenance(data_dir)
        return False, None
    if complete:
        return False, pending
    raise DatabaseFormatError(
        f"the restore of configuration backup {pending} did not finish; repeat that "
        "restore, or replace it by restoring every file of another backup (--all)"
    )


def _restore_problem(data_dir: Path, item: ConfigFile) -> str | None:
    if item.damaged:
        return "it is damaged in this backup; choose an earlier backup"
    # A backup reads only regular files reached through real directories
    # (``matching_data_paths``), so a restore writes nothing through a link
    # either: the before-restore backup could not hold what it would replace.
    parts = item.path.split("/")
    current = Path(data_dir)
    try:
        for depth, part in enumerate(parts, start=1):
            current = current / part
            status = stat_or_none(current, follow_symlinks=False)
            if status is not None and is_link_status(status):
                if depth == len(parts):
                    return "it is a link, which configuration backups neither read nor replace"
                folder = "/".join(parts[:depth])
                return f"its folder {folder} is a link, which configuration backups do not follow"
            if depth < len(parts) and (status is None or not stat.S_ISDIR(status.st_mode)):
                return (
                    "its folder no longer exists (a deleted or renamed Agent, Project or Channel)"
                )
    except OSError as exc:
        raise DatabaseUnavailableError(f"the folder of {item.path} cannot be checked") from exc
    return None


def _verified_object(root: Path, backup_id: str, item: ConfigFile) -> bytes:
    try:
        content = _object_path(root, item.sha256).read_bytes()
    except FileNotFoundError:
        raise DatabaseCorruptError(
            f"configuration backup {backup_id}: the copy of {item.path} is missing"
        ) from None
    except OSError as exc:
        raise DatabaseUnavailableError(
            f"configuration backup {backup_id}: the copy of {item.path} cannot be read"
        ) from exc
    if len(content) != item.file_size or hashlib.sha256(content).hexdigest() != item.sha256:
        raise DatabaseCorruptError(
            f"configuration backup {backup_id}: the copy of {item.path} was altered"
        )
    return content


def _before_restore_backup(
    data_dir: Path, backup_id: str, *, interrupted: bool, now: datetime | None
) -> str | None:
    """The backup of the state a restore replaces; taken once, also across a repeated restore.

    Its retention keeps the backup being restored, which an interrupted restore needs
    to be repeated: only that ends its maintenance guard.
    """
    if not interrupted:
        taken = _capture_config_backup_locked(
            data_dir, reason=f"before restore {backup_id}", now=now, keep=frozenset({backup_id})
        )
        if taken is not None:
            return taken.backup_id
    # The newest backup holds the replaced state: the current state equals it, or,
    # for a repeated restore, the interrupted attempt took it or found the state
    # equal to it, and no backup is created while that attempt's guard stands.
    newest = list_config_backups(data_dir)
    return newest[0].backup_id if newest else None
