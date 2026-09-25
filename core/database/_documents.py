"""The JSON document set of a data snapshot.

Besides one member per canonical database, a data snapshot holds every durable
JSON document of the data directory (``core.json_documents.DURABLE_DOCUMENTS``)
as one set member: ``<snapshot>/documents/<relative path>``, recorded in the
manifest by relative path, size and SHA-256.

The set is restored as a unit and only on request (an operator restore that
selects it, or the updater's rollback); corruption auto-restore never touches
it. Restoring makes the in-scope documents exactly the snapshot's: a document
whose content differs or that is missing is installed from the snapshot, and
a document the snapshot does not hold is removed. Every current document that
is replaced or removed first moves to a quarantine batch and is never deleted
automatically. The caller holds the maintenance guard, so an interrupted
restore keeps Runtime from starting until it is repeated.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.database._files import fsync_dir, fsync_file, sha256_file
from core.database.errors import DatabaseCorruptError, DatabaseUnavailableError
from core.json_documents import durable_document_paths, is_durable_document_path

DOCUMENTS_DIRECTORY_NAME = "documents"
#: The quarantine child of replaced documents; never a valid database name.
DOCUMENT_QUARANTINE_NAME = "json-documents"
_DOCUMENT_KEYS = frozenset({"file_size", "sha256"})
_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class DocumentMember:
    """One JSON document copy inside a data snapshot."""

    path: str
    file_size: int
    sha256: str


@dataclass(frozen=True)
class DocumentRestore:
    """The documents a set restore installs from the snapshot or removes.

    ``quarantine`` holds every current document that was replaced or removed;
    it is ``None`` for a plan and when nothing had to move.
    """

    restored: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    quarantine: Path | None = None

    @property
    def changed(self) -> bool:
        return bool(self.restored or self.removed)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _parts(path: str) -> list[str]:
    return path.split("/")


def _is_link(path: Path) -> bool:
    """A symbolic link or a Windows junction, as ``durable_document_paths`` skips them."""
    status = os.lstat(path)
    junction: int | None = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    tag: int | None = getattr(status, "st_reparse_tag", None)
    return stat.S_ISLNK(status.st_mode) or (junction is not None and tag == junction)


def _contained_file(root: Path, parts: list[str]) -> Path | None:
    """``root/parts`` when every component is a real directory or regular file."""
    current = root
    try:
        if _is_link(root):
            return None
        for part in parts:
            current = current / part
            if _is_link(current):
                return None
        if not stat.S_ISREG(os.lstat(current).st_mode):
            return None
    except (FileNotFoundError, NotADirectoryError):
        return None
    return current


def document_copy_path(snapshot_dir: Path, path: str) -> Path | None:
    """The snapshot copy of document ``path``, or ``None`` when it is absent or unsafe."""
    if not is_durable_document_path(path):
        return None
    try:
        return _contained_file(Path(snapshot_dir) / DOCUMENTS_DIRECTORY_NAME, _parts(path))
    except OSError as exc:
        raise DatabaseUnavailableError(f"snapshot document {path} is unavailable") from exc


def _data_path(data_dir: Path, path: str) -> Path:
    """The live location of a validated document path, through real directories only."""
    parts = _parts(path)
    current = Path(data_dir)
    for part in parts[:-1]:
        current = current / part
        try:
            if _is_link(current):
                raise DatabaseUnavailableError(
                    f"the directory of document {path} is a link; it is not restored"
                )
        except (FileNotFoundError, NotADirectoryError):
            break
    return Path(data_dir).joinpath(*parts)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def parse_documents(payload: object) -> dict[str, DocumentMember]:
    """Strictly parse the manifest's document set; malformed is corrupt."""
    if not isinstance(payload, dict):
        raise DatabaseCorruptError("snapshot manifest has an invalid document set")
    members: dict[str, DocumentMember] = {}
    folded: set[str] = set()
    for path, entry in payload.items():
        if not is_durable_document_path(path) or path.casefold() in folded:
            raise DatabaseCorruptError("snapshot manifest lists an invalid document path")
        folded.add(path.casefold())
        if not isinstance(entry, dict) or set(entry) != _DOCUMENT_KEYS:
            raise DatabaseCorruptError(f"snapshot document {path} has an unexpected shape")
        size, digest = entry["file_size"], entry["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise DatabaseCorruptError(f"snapshot document {path} has an invalid file_size")
        if not isinstance(digest, str) or _HEX64_PATTERN.fullmatch(digest) is None:
            raise DatabaseCorruptError(f"snapshot document {path} has an invalid sha256")
        members[path] = DocumentMember(path=path, file_size=size, sha256=digest)
    return members


def documents_payload(members: Mapping[str, DocumentMember]) -> dict[str, Any]:
    return {
        path: {"file_size": member.file_size, "sha256": member.sha256}
        for path, member in sorted(members.items())
    }


# ---------------------------------------------------------------------------
# Capture and verification
# ---------------------------------------------------------------------------


def capture_documents(
    data_dir: Path,
    snapshot_dir: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, DocumentMember] | None:
    """Copy every current document into an unpublished snapshot; ``None`` if cancelled.

    Each document is read whole in one short open, so a concurrent atomic
    replace by a running server sees at most a brief reader. The copy keeps the
    permission bits of its source (OAuth token files hold credentials). A
    document that disappears after listing is not a member.
    """
    root = Path(snapshot_dir) / DOCUMENTS_DIRECTORY_NAME
    members: dict[str, DocumentMember] = {}
    directories: set[Path] = set()
    for path in durable_document_paths(data_dir):
        if cancelled is not None and cancelled():
            return None
        try:
            with Path(data_dir).joinpath(*_parts(path)).open("rb") as source:
                permissions = stat.S_IMODE(os.fstat(source.fileno()).st_mode)
                content = source.read()
        except FileNotFoundError:
            continue
        destination = root.joinpath(*_parts(path))
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            permissions,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        directories.add(destination.parent)
        members[path] = DocumentMember(
            path=path, file_size=len(content), sha256=hashlib.sha256(content).hexdigest()
        )
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fsync_dir(directory)
    return members


def verify_documents(snapshot_dir: Path, members: Mapping[str, DocumentMember]) -> None:
    """Verify every document copy against its recorded size and hash."""
    for member in members.values():
        copy = document_copy_path(snapshot_dir, member.path)
        if copy is None:
            raise DatabaseCorruptError(f"snapshot document {member.path} is missing")
        try:
            if copy.stat().st_size != member.file_size:
                raise DatabaseCorruptError(f"snapshot document {member.path} size mismatch")
            if sha256_file(copy) != member.sha256:
                raise DatabaseCorruptError(f"snapshot document {member.path} hash mismatch")
        except FileNotFoundError as exc:
            raise DatabaseCorruptError(f"snapshot document {member.path} is missing") from exc
        except OSError as exc:
            raise DatabaseUnavailableError(
                f"snapshot document {member.path} is unavailable"
            ) from exc


def documents_present(snapshot_dir: Path, members: Mapping[str, DocumentMember]) -> bool:
    """Whether every document copy exists with its recorded size, without hashing."""
    for member in members.values():
        copy = document_copy_path(snapshot_dir, member.path)
        if copy is None or copy.stat().st_size != member.file_size:
            return False
    return True


def live_documents(data_dir: Path) -> dict[str, str]:
    """The SHA-256 of every current document in ``data_dir`` by relative path."""
    hashes: dict[str, str] = {}
    try:
        for path in durable_document_paths(data_dir):
            try:
                hashes[path] = sha256_file(Path(data_dir).joinpath(*_parts(path)))
            except FileNotFoundError:
                continue
    except OSError as exc:
        raise DatabaseUnavailableError("the JSON documents could not be read") from exc
    return hashes


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def plan_document_restore(data_dir: Path, members: Mapping[str, DocumentMember]) -> DocumentRestore:
    """What restoring ``members`` would install or remove, without changing anything."""
    current = live_documents(data_dir)
    return DocumentRestore(
        restored=tuple(
            sorted(path for path, member in members.items() if current.get(path) != member.sha256)
        ),
        removed=tuple(sorted(path for path in current if path not in members)),
    )


def restore_documents(
    data_dir: Path,
    snapshot_dir: Path,
    members: Mapping[str, DocumentMember],
    *,
    quarantine_batch: Path,
) -> DocumentRestore:
    """Make the current documents exactly ``members``; the caller holds the guard.

    Every copy is staged and verified before anything changes. Replaced and
    removed documents then move to ``quarantine_batch`` (all or rolled back),
    and the staged copies are published. A failure after that point leaves
    the guard in place; repeating the restore completes it.
    """
    data_dir = Path(data_dir)
    plan = plan_document_restore(data_dir, members)
    if not plan.changed:
        return plan
    staged: dict[str, Path] = {}
    try:
        for path in plan.restored:
            member = members[path]
            source = document_copy_path(snapshot_dir, path)
            if source is None:
                raise DatabaseCorruptError(f"snapshot document {path} is missing")
            target = _data_path(data_dir, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.restore.{uuid.uuid4().hex}.tmp")
            staged[path] = temporary
            shutil.copy2(source, temporary)
            if (
                temporary.stat().st_size != member.file_size
                or sha256_file(temporary) != member.sha256
            ):
                raise DatabaseCorruptError(f"snapshot document {path} changed while staged")
            fsync_file(temporary)
        displaced = [
            path
            for path in (*plan.restored, *plan.removed)
            if os.path.lexists(_data_path(data_dir, path))
        ]
        quarantine = (
            _quarantine_documents(data_dir, displaced, quarantine_batch) if displaced else None
        )
        for path, temporary in staged.items():
            target = _data_path(data_dir, path)
            os.replace(temporary, target)
            fsync_dir(target.parent)
    except (OSError, DatabaseCorruptError) as exc:
        raise DatabaseUnavailableError(
            f"the JSON document set could not be restored: {exc}"
        ) from exc
    finally:
        for temporary in staged.values():
            with suppress(OSError):
                temporary.unlink()
    return DocumentRestore(restored=plan.restored, removed=plan.removed, quarantine=quarantine)


def _quarantine_documents(data_dir: Path, paths: Iterable[str], batch: Path) -> Path:
    """Move current documents into one quarantine batch, or roll every move back."""
    moved: list[tuple[Path, Path]] = []
    try:
        batch.mkdir(parents=True, exist_ok=False)
        for path in paths:
            source = _data_path(data_dir, path)
            destination = batch.joinpath(*_parts(path))
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append((source, destination))
            fsync_dir(destination.parent)
        for source, _destination in moved:
            fsync_dir(source.parent)
    except OSError as exc:
        rollback_errors: list[str] = []
        for source, destination in reversed(moved):
            try:
                os.replace(destination, source)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        reason = f"the replaced JSON documents could not be quarantined: {exc}"
        if rollback_errors:
            reason += f"; rollback failed; retained documents at {batch}: " + "; ".join(
                rollback_errors
            )
        raise DatabaseUnavailableError(reason) from exc
    return batch
