"""Directory-tree moves that never put the last complete copy at risk.

``shutil.move`` renames when it can and otherwise copies the tree and then deletes
the source. When that deletion fails partway (an open handle or a read-only file on
Windows), the source is left half removed with no sign of which tree is whole, and a
caller that compensates by clearing the destination destroys the only complete copy.
:func:`move_tree` keeps one invariant instead: a raised error means nothing moved.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from core.utils.logging import get_logger

_LOGGER = get_logger("utils.tree_move")

# Tests patch this seam instead of the process-wide ``os.replace``.
_replace = os.replace


def move_tree(source: Path, destination: Path) -> None:
    """Move the directory ``source`` to ``destination``, which must not exist yet.

    The move is all or nothing: any raised error leaves ``source`` exactly as it was
    and creates no ``destination``, so a caller treats a failure as "not moved" and
    never has to guess which tree is complete. A refused rename (a program holding a
    file inside the tree open on Windows, a permission error) is such a failure; it
    never falls back to copying.

    Only a rename between volumes copies: the tree is copied first, then the
    original is renamed aside in one atomic step, which fails with the original
    intact while a program holds anything inside it, and only then deleted. A
    deletion that fails leaves the renamed leftovers next to ``source`` and a
    warning in the log; the move itself has succeeded.
    """
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, "The destination already exists", str(destination))
    if destination.resolve().is_relative_to(source.resolve()):
        raise OSError(errno.EINVAL, "Cannot move a directory into itself", str(destination))
    try:
        _replace(source, destination)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    _move_across_volumes(source, destination)


def _move_across_volumes(source: Path, destination: Path) -> None:
    # A same-directory rename is the one step guaranteed to stay on the source's volume.
    retired = source.with_name(f".{source.name}.moved-{uuid.uuid4().hex}")
    try:
        shutil.copytree(source, destination, symlinks=True)
        _replace(source, retired)
    except BaseException:
        # ``source`` is intact, so the destination is only a duplicate, whole or partial.
        _discard_tree(destination, "partial copy")
        raise
    _discard_tree(retired, "original")


def _discard_tree(path: Path, what: str) -> None:
    try:
        _remove_tree(path)
    except OSError as error:
        _LOGGER.warning("Could not remove the %s at %s: %s", what, path, error)


def _remove_tree(path: Path) -> None:
    """Delete a tree, clearing read-only attributes such as those of ``.git`` objects."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_read_only_and_retry)
    else:
        shutil.rmtree(path, onerror=_clear_read_only_and_retry)


def _clear_read_only_and_retry(action: Callable[[str], object], path: str, _error: object) -> None:
    os.chmod(path, os.lstat(path).st_mode | stat.S_IWRITE)
    action(path)
