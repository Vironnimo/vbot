"""Content evidence for reusing an offline snapshot of an unchanged data directory."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import core.json_documents as json_documents
from core.database._snapshot_verification import _sha256
from core.database.errors import DatabaseUnavailableError
from core.database.marker import marker_path, read_marker
from core.database.spec import canonical_relative_path

type SourceState = dict[str, tuple[int, int, int, int, int] | None]


def _state(data_dir: Path) -> SourceState:
    marker = read_marker(data_dir)
    paths = {marker_path(data_dir).relative_to(data_dir).as_posix()}
    journals: set[str] = set()
    for name in marker.databases if marker is not None else ():
        relative = canonical_relative_path(name).as_posix()
        paths.add(relative)
        journals.update(relative + suffix for suffix in ("-wal", "-journal"))
    paths.update(journals)
    paths.update(json_documents.snapshot_document_paths(data_dir))
    result: SourceState = {}
    for relative in sorted(paths):
        try:
            status = (data_dir / relative).stat()
        except FileNotFoundError:
            if relative not in journals:
                raise
            result[relative] = None
            continue
        result[relative] = (
            None
            if relative in journals and status.st_size == 0
            else (
                status.st_size,
                status.st_mtime_ns,
                status.st_ctime_ns,
                stat.S_IMODE(status.st_mode),
                status.st_ino,
            )
        )
    return result


@dataclass(frozen=True)
class OfflineSnapshotSource:
    """A content fingerprint plus temporary evidence that capture stayed offline."""

    fingerprint: str
    state: SourceState

    def require_unchanged(self, data_dir: Path) -> None:
        if self.state != _state(data_dir):
            raise DatabaseUnavailableError(
                "the data changed while the offline snapshot was checked"
            )


def read_offline_source(
    data_dir: Path, *, cancelled: Callable[[], bool] | None = None
) -> OfflineSnapshotSource:
    """Hash every source file, including journals and document permissions.

    Metadata only detects a concurrent write; equality across updates depends
    on bytes, paths, permissions and the current document catalog, not mtimes.
    Empty journals and absent journals both hold no data. SQLite shared-memory
    files are coordination state and are never restored.
    """
    state = _state(data_dir)
    contents = {
        relative: None
        if status is None
        else (_sha256(data_dir / relative, cancelled=cancelled), status[3])
        for relative, status in state.items()
    }
    payload = ["offline-source-v1", sorted(json_documents.SNAPSHOT_DOCUMENTS.values()), contents]
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    source = OfflineSnapshotSource(fingerprint, state)
    source.require_unchanged(data_dir)
    return source
