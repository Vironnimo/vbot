"""Configuration backups: frequent, deduplicated copies of the configuration files.

A data snapshot copies every canonical database and is therefore taken rarely.
The configuration of a data directory is small and changes often, so it is
backed up on its own: the JSON document set of data snapshots
(``core.json_documents.SNAPSHOT_DOCUMENTS``) plus
:data:`CONFIGURATION_FILE_PATTERNS` (the data-directory ``.env``, the identity
files of Workspaces in their default location, and prompt overrides).

Layout under ``<data-dir>/config-backups/``:

- ``objects/<aa>/<sha256>``: each distinct file content once, so a backup of
  unchanged files costs only its manifest;
- ``backups/<backup-id>.json``: one manifest per backup naming every file with
  its hash, size and permission bits, and marking JSON documents that did not
  parse as ``damaged``.

A backup is taken only when the files differ from the newest backup, inside the
data snapshot freeze, so the documents of one backup agree like those of a data
snapshot. Retention keeps the newest :data:`CONFIG_BACKUP_KEEP_LATEST` backups
and the newest backup of every hour, day and ISO week within the tier horizons,
so a run of bad states never pushes the good ones out. Restore is offline
maintenance: it replaces the selected files (never one damaged in the backup,
never one whose folder no longer exists), leaves files created after the backup
alone, and first backs up the current state, so a restore can be taken back by
restoring that backup.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

# A module import: ``core.json_documents`` imports the kernel's snapshot freeze in turn.
import core.json_documents as json_documents
from core.database._files import fsync_dir
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    OperationLockBusyError,
)
from core.database.marker import (
    acquire_operation_lock,
    maintenance,
    read_maintenance,
    require_no_maintenance,
)
from core.database.snapshot_barrier import capture_members
from core.utils.atomic import atomic_write_bytes, atomic_write_text
from core.utils.file_status import is_dir_strict
from core.utils.log_conditions import LoggedConditions
from core.utils.timestamps import (
    format_canonical_timestamp,
    is_canonical_timestamp,
    parse_canonical_timestamp,
)
from core.utils.version import detect_vbot_version

if TYPE_CHECKING:
    from core.database.snapshot_barrier import SnapshotBarrier

_LOGGER = logging.getLogger("vbot.database")

CONFIG_BACKUP_ROOT_NAME = "config-backups"
CONFIG_BACKUP_MANIFEST_VERSION = 1
#: The maintenance operation a configuration restore holds, followed by the backup id.
CONFIG_RESTORE_OPERATION = "config restore"
#: Configuration files beside the JSON document set, relative to the data directory.
CONFIGURATION_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    "agents/*/workspace/SOUL.md",
    "agents/*/workspace/USER.md",
    "agents/*/workspace/MEMORY.md",
    "prompts/*.md",
    "prompts/blocks/*/*.md",
    "agents/*/prompts/*.md",
    "agents/*/prompts/blocks/*/*.md",
)
CONFIG_BACKUP_KEEP_LATEST = 10
CONFIG_BACKUP_KEEP_HOURLY = timedelta(hours=48)
CONFIG_BACKUP_KEEP_DAILY = timedelta(days=30)
CONFIG_BACKUP_KEEP_WEEKLY = timedelta(weeks=52)
#: How long a backup waits for another data-store operation, such as a data snapshot.
CONFIG_BACKUP_LOCK_SECONDS = 5.0

_BACKUPS_DIRECTORY = "backups"
_OBJECTS_DIRECTORY = "objects"
_BACKUP_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = frozenset(
    {"manifest_version", "backup_id", "created_at", "reason", "vbot_version", "files"}
)
_FILE_KEYS = frozenset({"sha256", "file_size", "mode"})
_UNREADABLE_MANIFESTS = LoggedConditions()


@dataclass(frozen=True)
class ConfigFile:
    """One configuration file inside a configuration backup."""

    path: str
    sha256: str
    file_size: int
    mode: int
    #: A JSON document that did not parse as a JSON object; never restored.
    damaged: bool = False


@dataclass(frozen=True)
class ConfigBackup:
    """One configuration backup, read from its manifest.

    ``files`` holds only the files this vBot backs up; ``other_objects`` are the
    contents of further files a newer vBot recorded in the manifest, which this
    vBot neither shows nor restores but must keep.
    """

    backup_id: str
    created_at: str
    reason: str
    vbot_version: str
    files: Mapping[str, ConfigFile]
    other_objects: frozenset[str] = frozenset()

    def objects(self) -> frozenset[str]:
        """Every stored content the manifest references."""
        return frozenset(item.sha256 for item in self.files.values()) | self.other_objects

    def created_instant(self) -> datetime:
        return parse_canonical_timestamp(self.created_at)

    def damaged(self) -> tuple[str, ...]:
        return tuple(sorted(path for path, item in self.files.items() if item.damaged))

    def summary(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "reason": self.reason,
            "vbot_version": self.vbot_version,
            "files": len(self.files),
            "damaged": list(self.damaged()),
        }


@dataclass(frozen=True)
class ConfigRestore:
    """What a configuration restore changed, or would change for ``check_only``.

    ``skipped`` maps a file of the backup that was left alone to the reason;
    ``created_after`` lists current configuration files the backup does not
    hold, which a restore never removes. ``before_restore`` is the backup that
    holds the state before the restore (``None`` for a plan or when nothing
    changed). ``interrupted`` says that an earlier restore of this backup did
    not finish; the restore finishes it even when no file differs any more.
    """

    backup_id: str
    restored: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    skipped: Mapping[str, str] = field(default_factory=dict)
    created_after: tuple[str, ...] = ()
    before_restore: str | None = None
    interrupted: bool = False


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def config_backup_root(data_dir: Path) -> Path:
    return Path(data_dir) / CONFIG_BACKUP_ROOT_NAME


def _patterns() -> tuple[str, ...]:
    return (*json_documents.SNAPSHOT_DOCUMENTS.values(), *CONFIGURATION_FILE_PATTERNS)


def configuration_paths(data_dir: Path) -> tuple[str, ...]:
    """Every existing configuration file as a relative POSIX path; ``OSError`` when unlistable."""
    return json_documents.matching_data_paths(data_dir, _patterns())


def is_configuration_path(path: str) -> bool:
    return json_documents.path_matches_patterns(path, _patterns())


def configuration_fingerprint(data_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """Path, size and modification time of every configuration file: a cheap change check."""
    stamps: list[tuple[str, int, int]] = []
    for path in configuration_paths(data_dir):
        try:
            status = _data_path(data_dir, path).stat()
        except FileNotFoundError:
            continue
        stamps.append((path, status.st_size, status.st_mtime_ns))
    return tuple(stamps)


def _data_path(data_dir: Path, path: str) -> Path:
    return Path(data_dir).joinpath(*path.split("/"))


def _object_path(root: Path, digest: str) -> Path:
    return root / _OBJECTS_DIRECTORY / digest[:2] / digest


def _manifest_path(root: Path, backup_id: str) -> Path:
    return root / _BACKUPS_DIRECTORY / f"{backup_id}.json"


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


def _manifest_payload(backup: ConfigBackup) -> dict[str, Any]:
    return {
        "manifest_version": CONFIG_BACKUP_MANIFEST_VERSION,
        "backup_id": backup.backup_id,
        "created_at": backup.created_at,
        "reason": backup.reason,
        "vbot_version": backup.vbot_version,
        "files": {
            path: {
                "sha256": item.sha256,
                "file_size": item.file_size,
                "mode": item.mode,
                "damaged": item.damaged,
            }
            for path, item in sorted(backup.files.items())
        },
    }


def _parse_manifest(raw: bytes, backup_id: str) -> ConfigBackup:
    """Parse one manifest; malformed is corrupt.

    Unknown fields are ignored, and so is a well-formed path that this vBot does
    not back up (a newer vBot added it): this vBot neither shows nor restores it,
    but keeps its content (``other_objects``).
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatabaseCorruptError(f"configuration backup {backup_id} is not valid JSON") from exc
    if not isinstance(payload, dict) or not set(payload) >= _MANIFEST_KEYS:
        raise DatabaseCorruptError(f"configuration backup {backup_id} has an unexpected shape")
    if payload["manifest_version"] != CONFIG_BACKUP_MANIFEST_VERSION:
        raise DatabaseCorruptError(
            f"configuration backup {backup_id} has manifest version {payload['manifest_version']!r}"
        )
    if payload["backup_id"] != backup_id:
        raise DatabaseCorruptError(f"configuration backup {backup_id} names another backup")
    created_at, reason, version = payload["created_at"], payload["reason"], payload["vbot_version"]
    if not isinstance(created_at, str) or not is_canonical_timestamp(created_at):
        raise DatabaseCorruptError(f"configuration backup {backup_id} has an invalid created_at")
    if not isinstance(reason, str) or not isinstance(version, str):
        raise DatabaseCorruptError(f"configuration backup {backup_id} has an unexpected shape")
    entries = payload["files"]
    if not isinstance(entries, dict):
        raise DatabaseCorruptError(f"configuration backup {backup_id} has an invalid file list")
    files: dict[str, ConfigFile] = {}
    other_objects: set[str] = set()
    for path, entry in entries.items():
        if not isinstance(entry, dict) or not set(entry) >= _FILE_KEYS:
            raise DatabaseCorruptError(f"configuration backup {backup_id}: {path} is malformed")
        digest, size, mode = entry["sha256"], entry["file_size"], entry["mode"]
        damaged = entry.get("damaged", False)
        if (
            not isinstance(digest, str)
            or _HEX64_PATTERN.fullmatch(digest) is None
            or not _is_count(size)
            or not _is_count(mode)
            or not isinstance(damaged, bool)
        ):
            raise DatabaseCorruptError(f"configuration backup {backup_id}: {path} is malformed")
        if is_configuration_path(path):
            files[path] = ConfigFile(path, digest, size, mode, damaged)
        else:
            other_objects.add(digest)
    return ConfigBackup(backup_id, created_at, reason, version, files, frozenset(other_objects))


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _scan(data_dir: Path) -> tuple[list[ConfigBackup], int]:
    """Every readable backup, newest first, and how many manifests could not be read."""
    directory = config_backup_root(data_dir) / _BACKUPS_DIRECTORY
    backups: list[ConfigBackup] = []
    unreadable = 0
    try:
        names = sorted(entry.name for entry in os.scandir(directory) if entry.is_file())
    except FileNotFoundError:
        return [], 0
    except OSError as exc:
        raise DatabaseUnavailableError("the configuration backups cannot be listed") from exc
    for name in names:
        backup_id = name.removesuffix(".json")
        if name == backup_id or _BACKUP_ID_PATTERN.fullmatch(backup_id) is None:
            continue
        try:
            backups.append(_parse_manifest((directory / name).read_bytes(), backup_id))
        except FileNotFoundError:
            continue
        except (OSError, DatabaseCorruptError) as exc:
            unreadable += 1
            if _UNREADABLE_MANIFESTS.started((directory / name).as_posix(), str(exc)):
                _LOGGER.warning("Configuration backup cannot be read; it is left alone: %s", exc)
            continue
        _UNREADABLE_MANIFESTS.ended((directory / name).as_posix())
    backups.sort(key=lambda backup: (backup.created_at, backup.backup_id), reverse=True)
    return backups, unreadable


def list_config_backups(data_dir: Path) -> list[ConfigBackup]:
    """Every readable configuration backup, newest first."""
    return _scan(data_dir)[0]


def read_config_backup(data_dir: Path, backup_id: str) -> ConfigBackup:
    """One configuration backup; ``ValueError`` when there is none with ``backup_id``."""
    if not isinstance(backup_id, str) or _BACKUP_ID_PATTERN.fullmatch(backup_id) is None:
        raise ValueError(f"not a configuration backup id: {backup_id!r}")
    path = _manifest_path(config_backup_root(data_dir), backup_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ValueError(f"there is no configuration backup {backup_id}") from None
    except OSError as exc:
        raise DatabaseUnavailableError(f"configuration backup {backup_id} cannot be read") from exc
    return _parse_manifest(raw, backup_id)


def config_backup_summary(data_dir: Path) -> dict[str, Any]:
    """Count and newest configuration backup, for the data-store status."""
    try:
        backups, unreadable = _scan(data_dir)
    except DatabaseUnavailableError as exc:
        return {"count": 0, "latest": None, "unreadable": 0, "reason": str(exc)}
    return {
        "count": len(backups),
        "latest": backups[0].summary() if backups else None,
        "unreadable": unreadable,
        "reason": None,
    }


def describe_config_backup(data_dir: Path, backup_id: str) -> dict[str, Any]:
    """One backup with the state of each of its files against the current one.

    A file is ``same``, ``differs`` or ``missing`` (absent now); ``created_after``
    lists current configuration files the backup does not hold.
    """
    backup = read_config_backup(data_dir, backup_id)
    current = _current_hashes(data_dir)
    files: dict[str, dict[str, Any]] = {}
    for path, item in sorted(backup.files.items()):
        digest = current.get(path)
        state = "missing" if digest is None else "same" if digest == item.sha256 else "differs"
        files[path] = {"state": state, "damaged": item.damaged}
    return {
        "backup": backup.summary(),
        "files": files,
        "created_after": sorted(path for path in current if path not in backup.files),
    }


def _current_hashes(data_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    try:
        for path in configuration_paths(data_dir):
            try:
                hashes[path] = hashlib.sha256(_data_path(data_dir, path).read_bytes()).hexdigest()
            except FileNotFoundError:
                continue
    except OSError as exc:
        raise DatabaseUnavailableError("the configuration files cannot be read") from exc
    return hashes


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture_config_backup(
    data_dir: Path,
    *,
    reason: str,
    barrier: SnapshotBarrier | None = None,
    now: datetime | None = None,
) -> ConfigBackup | None:
    """Back up the configuration files unless they equal the newest backup.

    Returns the new backup, or ``None`` when nothing changed. ``barrier`` is the
    running Runtime's snapshot barrier, which keeps compound mutations whole.
    Raises ``DatabaseFormatError`` while data maintenance is incomplete,
    :class:`OperationLockBusyError` while another data-store operation runs,
    ``DatabaseUnavailableError`` when the files could not be held still, and
    ``OSError`` when they cannot be read or the backup cannot be written.
    """
    if not reason or not reason.strip():
        raise ValueError("configuration backup reason must be non-empty")
    data_dir = Path(data_dir)
    require_no_maintenance(data_dir)
    lock = acquire_operation_lock(data_dir, CONFIG_BACKUP_LOCK_SECONDS)
    if lock is None:
        raise OperationLockBusyError("another data-store operation holds the operation lock")
    try:
        contents = _read_configuration(data_dir, barrier)
        files = {
            path: ConfigFile(
                path=path,
                sha256=hashlib.sha256(content).hexdigest(),
                file_size=len(content),
                mode=mode,
                damaged=_is_damaged(path, content),
            )
            for path, (content, mode) in contents.items()
        }
        backups, unreadable = _scan(data_dir)
        if backups and backups[0].files == files:
            return None
        moment = datetime.now(UTC) if now is None else now
        root = config_backup_root(data_dir)
        for path, (content, _mode) in sorted(contents.items()):
            _store_object(root, files[path].sha256, content)
        backup = ConfigBackup(
            backup_id=f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
            created_at=format_canonical_timestamp(moment),
            reason=reason,
            vbot_version=detect_vbot_version(),
            files=files,
        )
        manifest = _manifest_path(root, backup.backup_id)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(manifest, json.dumps(_manifest_payload(backup), indent=2) + "\n")
        fsync_dir(manifest.parent)
        _prune(root, [backup, *backups], unreadable=unreadable, now=moment)
        _LOGGER.info(
            "Created configuration backup (backup=%s files=%d changed=%d reason=%s)",
            backup.backup_id,
            len(files),
            _changed_count(backups[0].files if backups else {}, files),
            reason,
        )
        if damaged := backup.damaged():
            _LOGGER.warning(
                "Configuration backup holds JSON documents that do not parse; they are "
                "never restored from it (backup=%s documents=%s)",
                backup.backup_id,
                ",".join(damaged),
            )
        return backup
    finally:
        lock.release()


def _read_configuration(
    data_dir: Path, barrier: SnapshotBarrier | None
) -> dict[str, tuple[bytes, int]]:
    """Read every configuration file inside the data snapshot freeze."""
    contents: dict[str, tuple[bytes, int]] = {}

    def read_files() -> None:
        contents.clear()
        for path in configuration_paths(data_dir):
            try:
                with _data_path(data_dir, path).open("rb") as source:
                    mode = stat.S_IMODE(os.fstat(source.fileno()).st_mode)
                    contents[path] = (source.read(), mode)
            except FileNotFoundError:
                continue

    def no_database(name: str) -> None:
        raise AssertionError(f"a configuration backup copies no database: {name}")

    capture = capture_members(
        data_dir,
        {},
        copy_database=no_database,
        copy_documents=read_files,
        discard_copies=contents.clear,
        barrier=barrier,
    )
    if capture is None:
        raise DatabaseUnavailableError("the configuration backup was cancelled")
    return contents


def _is_damaged(path: str, content: bytes) -> bool:
    """A JSON document that does not parse as a JSON object, such as a torn or zeroed file."""
    if not json_documents.is_snapshot_document_path(path):
        return False
    try:
        return not isinstance(json.loads(content.decode("utf-8")), dict)
    except UnicodeDecodeError, json.JSONDecodeError:
        return True


def _changed_count(previous: Mapping[str, ConfigFile], current: Mapping[str, ConfigFile]) -> int:
    return sum(
        1 for path in previous.keys() | current.keys() if previous.get(path) != current.get(path)
    )


def _store_object(root: Path, digest: str, content: bytes) -> None:
    """Store one content durably under its hash; an existing complete object stays."""
    target = _object_path(root, digest)
    try:
        if target.stat().st_size == len(content):
            return
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
    # Objects can hold credentials (.env, OAuth tokens): owner-only from creation.
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_dir(target.parent)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _retained(backups: list[ConfigBackup], now: datetime) -> set[str]:
    """The backups retention keeps: the newest ones, then the newest of each tier bucket."""
    keep = {backup.backup_id for backup in backups[:CONFIG_BACKUP_KEEP_LATEST]}
    tiers: tuple[tuple[timedelta, Any], ...] = (
        (CONFIG_BACKUP_KEEP_HOURLY, lambda moment: (moment.date(), moment.hour)),
        (CONFIG_BACKUP_KEEP_DAILY, lambda moment: moment.date()),
        (CONFIG_BACKUP_KEEP_WEEKLY, lambda moment: moment.isocalendar()[:2]),
    )
    for horizon, bucket in tiers:
        seen: set[Any] = set()
        for backup in backups:
            created = backup.created_instant()
            if now - created > horizon:
                continue
            key = bucket(created)
            if key not in seen:
                seen.add(key)
                keep.add(backup.backup_id)
    return keep


def _prune(root: Path, backups: list[ConfigBackup], *, unreadable: int, now: datetime) -> None:
    """Remove the backups retention drops, then the objects no backup references.

    Objects are kept while any manifest cannot be read, since it may reference them.
    """
    backups = sorted(
        backups, key=lambda backup: (backup.created_at, backup.backup_id), reverse=True
    )
    keep = _retained(backups, now)
    for backup in backups:
        if backup.backup_id not in keep:
            _manifest_path(root, backup.backup_id).unlink(missing_ok=True)
    if unreadable:
        return
    referenced = {
        digest for backup in backups if backup.backup_id in keep for digest in backup.objects()
    }
    try:
        with os.scandir(root / _OBJECTS_DIRECTORY) as listing:
            directories = [entry for entry in listing if entry.is_dir()]
    except FileNotFoundError:
        return
    for directory in directories:
        with os.scandir(directory.path) as entries:
            for entry in entries:
                if entry.name not in referenced:
                    Path(entry.path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def restore_config_backup(
    data_dir: Path,
    backup_id: str,
    *,
    paths: Iterable[str] | None = None,
    check_only: bool = False,
    now: datetime | None = None,
) -> ConfigRestore:
    """Restore files of one configuration backup while no server runs on ``data_dir``.

    ``paths`` names the files to restore (``None``: every file of the backup).
    A named file that is not in the backup, is damaged there, or whose folder
    no longer exists (a deleted or renamed Agent, Project or Channel) refuses
    the restore with ``ValueError``; restoring every file skips such files
    instead. Every object is verified before anything changes; a missing or
    altered one raises ``DatabaseCorruptError``. Files created after the backup
    stay. Before the first change the current state is backed up and the
    maintenance guard held, so an interrupted restore keeps Runtime from
    starting until it is repeated; while it is incomplete, any other restore
    raises ``DatabaseFormatError``.
    """
    data_dir = Path(data_dir)
    backup = read_config_backup(data_dir, backup_id)
    operation = _restore_operation(backup_id)
    interrupted = _interrupted_restore(data_dir, operation)
    explicit = paths is not None
    selected = (
        sorted(backup.files)
        if paths is None
        else sorted({path.replace("\\", "/") for path in paths})
    )
    if explicit and not selected:
        raise ValueError("name at least one file to restore")
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
    )
    if check_only or not (restored or interrupted):
        return plan
    before = _before_restore_backup(data_dir, backup_id, interrupted=interrupted, now=now)
    # An interrupted attempt may have written every file already: entering and
    # leaving the guard finishes it.
    with maintenance(data_dir, operation, resume=True):
        for path, (content, item) in restored.items():
            atomic_write_bytes(
                _data_path(data_dir, path),
                content,
                # A read-only file could not be written through its own bits.
                mode=item.mode if os.name == "posix" and item.mode & stat.S_IWUSR else None,
            )
    _LOGGER.info(
        "Restored configuration backup (backup=%s files=%d before_restore=%s)",
        backup_id,
        len(restored),
        before,
    )
    return ConfigRestore(
        backup_id=backup_id,
        restored=plan.restored,
        unchanged=plan.unchanged,
        skipped=skipped,
        created_after=plan.created_after,
        before_restore=before,
        interrupted=interrupted,
    )


def _restore_operation(backup_id: str) -> str:
    return f"{CONFIG_RESTORE_OPERATION} {backup_id}"


def _interrupted_restore(data_dir: Path, operation: str) -> bool:
    """Whether a restore of this backup is incomplete; refuse any other incomplete operation."""
    guard = read_maintenance(data_dir)
    if guard is None:
        return False
    if guard.operation == operation:
        return True
    pending = guard.operation.removeprefix(f"{CONFIG_RESTORE_OPERATION} ")
    if pending != guard.operation:
        raise DatabaseFormatError(
            f"the restore of configuration backup {pending} did not finish; "
            "repeat that restore first"
        )
    require_no_maintenance(data_dir)
    return False


def _restore_problem(data_dir: Path, item: ConfigFile) -> str | None:
    if item.damaged:
        return "it is damaged in this backup; choose an earlier backup"
    try:
        folder_exists = is_dir_strict(_data_path(data_dir, item.path).parent)
    except OSError as exc:
        raise DatabaseUnavailableError(f"the folder of {item.path} cannot be checked") from exc
    if not folder_exists:
        return "its folder no longer exists (a deleted or renamed Agent, Project or Channel)"
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
    """The backup of the state a restore replaces; taken once, also across a repeated restore."""
    reason = f"before restore {backup_id}"
    if interrupted:
        # A repeated restore: the interrupted attempt took the backup already.
        earlier = [backup for backup in list_config_backups(data_dir) if backup.reason == reason]
        return earlier[0].backup_id if earlier else None
    taken = capture_config_backup(data_dir, reason=reason, now=now)
    if taken is not None:
        return taken.backup_id
    # The current state equals the newest backup, which therefore holds it.
    newest = list_config_backups(data_dir)
    return newest[0].backup_id if newest else None
