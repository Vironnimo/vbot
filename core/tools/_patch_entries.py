"""Filesystem entries an apply_patch call observes, resolves, and renames.

Add and Update change file content, so their paths resolve through links to the
target file. Delete and Move act on the named directory entry: a final symbolic
link or Windows junction is deleted or moved itself, never its target. A snapshot
records a regular file's bytes and permission bits, or a link by its target text,
so drift checks compare exactly the entry a later step would change.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from core.tools._patch_syntax import _PatchError
from core.tools._tool_context import is_link_entry
from core.tools.tools import ToolContext


@dataclass(frozen=True)
class _Snapshot:
    payload: bytes | None
    mode: int | None = None
    link: str | None = None

    @property
    def exists(self) -> bool:
        return self.payload is not None or self.link is not None


_ABSENT = _Snapshot(None)


def _snapshot(path: Path) -> _Snapshot:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _ABSENT
    if is_link_entry(path):
        return _Snapshot(None, link=os.readlink(path))
    if not stat.S_ISREG(info.st_mode):
        raise _PatchError("not_a_file", path=path)
    return _Snapshot(path.read_bytes(), stat.S_IMODE(info.st_mode))


def _resolve(context: ToolContext, path: str, *, follow_final_link: bool = True) -> Path:
    try:
        if "\x00" in path:
            raise ValueError(path)
        return context.resolve_path(path, follow_final_link=follow_final_link)
    except (ValueError, OSError, RuntimeError) as error:
        raise _PatchError("invalid_path", path=path, reason=str(error)) from error


def _entry_path(context: ToolContext, name: str, content: Path, *, destination: bool) -> Path:
    """Return the entry a Delete/Move names instead of the file behind a final link.

    A move destination keeps its requested final-name spelling, so a rename that
    only changes letter case still names a different entry on Windows.
    """
    entry = _resolve(context, name, follow_final_link=False)
    return entry if destination or is_link_entry(entry) else content


def _renames_entry(source: Path, destination: Path, snapshot: _Snapshot) -> bool:
    """Return whether a move must rename the entry instead of copying content.

    A link moves as itself. A destination naming the same entry with another
    spelling (a case-only rename on Windows) cannot be copied onto its source.
    """
    return snapshot.link is not None or (source == destination and source.name != destination.name)


def _rename_entry(source: Path, destination: Path, before: dict[Path, _Snapshot]) -> None:
    """Rename ``source`` to ``destination`` after rechecking both entries.

    Precondition failures raise ``_PatchError`` before any effect; an ``OSError``
    means the rename itself did not happen.
    """
    snapshot = before[source]
    if not snapshot.exists:
        raise _PatchError("file_not_found", path=source)
    if destination != source:
        if before[destination].exists:
            raise _PatchError("destination_exists", path=destination)
    elif os.path.lexists(destination) and not os.path.samestat(
        os.lstat(source), os.lstat(destination)
    ):
        # A case-sensitive directory can hold both spellings as distinct files.
        raise _PatchError("destination_exists", path=destination)
    if _snapshot(source) != snapshot:
        raise _PatchError("file_changed", path=source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, destination)


def _observe_renamed(destination: Path, expected: _Snapshot) -> _Snapshot:
    """Verify a completed rename: same entry, now listed under the new spelling."""
    actual = _snapshot(destination)
    if actual != expected or destination.name not in os.listdir(destination.parent):
        raise _PatchError("file_changed", path=destination)
    return actual
