"""Staging of data-store operations, and its removal after an interruption.

A data snapshot, a restore and the creation of a canonical database each build
their result under a staging name that starts with ``.`` beside its final
location, and publish it with one atomic rename:

- a snapshot: ``snapshots/.<snapshot-id>.<pid>.partial/``
- a restored database or JSON document: ``.<file>.restore.<hex>.tmp`` beside it
- a new database: ``.<file>.<hex>.creating`` beside it, with SQLite's sidecars

Each attempt holds the data-store operation lock while its staging exists and
removes the staging when it fails, so under that lock any staging found belongs
to an attempt that was killed, and :func:`remove_staging_locked` removes it. A
disposable database is created the same way without the lock, but never at a
canonical database location, which is the only place that function removes
creation staging from.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import stat
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

# A module import: ``core.json_documents`` imports the kernel's snapshot freeze in turn.
import core.json_documents as json_documents
from core.database.spec import EXTENSION_DATA_ROOT_NAME, canonical_database_name
from core.utils.file_status import is_link_status
from core.utils.tree_move import remove_tree

_LOGGER = logging.getLogger("vbot.database")

_SNAPSHOT_STAGING = re.compile(r"^\.[^.]+\.\d+\.partial$")
_RESTORE_STAGING = re.compile(r"^\.(?P<target>.+)\.restore\.[0-9a-f]{32}\.tmp$")
_CREATION_STAGING = re.compile(r"^\.(?P<target>.+)\.[0-9a-f]{32}\.creating(?:-journal|-wal|-shm)?$")


def snapshot_staging_path(root: Path, snapshot_id: str) -> Path:
    """Where snapshot ``snapshot_id`` is captured before it is published in ``root``."""
    return root / f".{snapshot_id}.{os.getpid()}.partial"


def restore_staging_path(target: Path) -> Path:
    """A new place to stage the restored bytes of ``target``."""
    return target.with_name(f".{target.name}.restore.{uuid.uuid4().hex}.tmp")


def creation_staging_path(path: Path) -> Path:
    """A new place to build the database that is then published at ``path``."""
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.creating")


def remove_staging_locked(data_dir: Path, *, snapshot_root: Path) -> None:
    """Remove the staging of every interrupted attempt; the caller holds the operation lock.

    What cannot be listed or removed is logged and stays for the next call, so
    filesystem failures never raise here.
    """
    data_dir = Path(data_dir)
    leftovers = [
        (snapshot_root / name, snapshot_root)
        for name in _names(snapshot_root)
        if _SNAPSHOT_STAGING.fullmatch(name)
    ]
    for directory in sorted(_staging_directories(data_dir)):
        parts = directory.relative_to(data_dir).parts
        leftovers.extend(
            (directory / name, data_dir) for name in _names(directory) if _is_staging(parts, name)
        )
    removed: list[str] = []
    for path, within in leftovers:
        try:
            remove_tree(path, within=within)
        except OSError as exc:
            _LOGGER.warning("Interrupted staging could not be removed (path=%s): %s", path, exc)
            continue
        removed.append(path.relative_to(data_dir).as_posix())
    if removed:
        _LOGGER.info(
            "Removed staging left by interrupted data-store operations (paths=%s)",
            ",".join(removed),
        )


def _is_staging(parts: Sequence[str], name: str) -> bool:
    """Whether ``name`` in the directory ``parts`` stages a canonical database or document."""
    if (match := _RESTORE_STAGING.fullmatch(name)) is not None:
        target = "/".join((*parts, match["target"]))
        return canonical_database_name(target) is not None or (
            json_documents.is_snapshot_document_path(target)
        )
    if (match := _CREATION_STAGING.fullmatch(name)) is not None:
        return canonical_database_name("/".join((*parts, match["target"]))) is not None
    return False


def _staging_directories(data_dir: Path) -> set[Path]:
    """The real directories that hold canonical databases or snapshot documents."""
    patterns = [
        (),
        (EXTENSION_DATA_ROOT_NAME, "*"),
        *(tuple(pattern.split("/")[:-1]) for pattern in json_documents.SNAPSHOT_DOCUMENTS.values()),
    ]
    found: set[Path] = set()
    for parts in patterns:
        found.update(_directories(data_dir, parts))
    return found


def _directories(directory: Path, parts: Sequence[str]) -> Iterator[Path]:
    """The real directories below ``directory`` that ``parts`` match as document patterns do."""
    if not parts:
        yield directory
        return
    segment, rest = parts[0], parts[1:]
    if any(char in segment for char in "*?["):
        names = [
            name
            for name in _names(directory)
            if not name.startswith(".") and fnmatch.fnmatchcase(name, segment)
        ]
    else:
        names = [segment]
    for name in names:
        try:
            status = os.lstat(directory / name)
        except OSError:
            continue
        if stat.S_ISDIR(status.st_mode) and not is_link_status(status):
            yield from _directories(directory / name, rest)


def _names(directory: Path) -> list[str]:
    """The entry names in ``directory``; none when it is missing or cannot be listed."""
    try:
        with os.scandir(directory) as entries:
            return [entry.name for entry in entries]
    except FileNotFoundError, NotADirectoryError:
        return []
    except OSError as exc:
        _LOGGER.warning(
            "Directory could not be checked for interrupted staging (path=%s): %s", directory, exc
        )
        return []
