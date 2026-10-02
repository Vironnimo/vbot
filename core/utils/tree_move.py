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
        _copy_tree(source, destination)
        _replace(source, retired)
    except BaseException:
        # ``source`` is intact, so the destination is only a duplicate, whole or partial.
        _discard_tree(destination, "partial copy")
        raise
    _discard_tree(retired, "original")


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy the tree ``source`` as a rename moves it: every link stays a link to its target.

    ``shutil.copytree`` keeps symbolic links but copies what a Windows junction
    points at, duplicating a tree from outside ``source``. Junctions are left out
    of the copy and recreated with their targets afterwards instead.
    """
    if sys.platform != "win32":
        shutil.copytree(source, destination, symlinks=True)
        return
    import _winapi

    junctions: list[tuple[Path, str]] = []

    def leave_out_junctions(directory: str, names: list[str]) -> set[str]:
        left_out = {name for name in names if os.path.isjunction(os.path.join(directory, name))}
        junctions.extend(
            (Path(directory, name).relative_to(source), os.readlink(Path(directory, name)))
            for name in sorted(left_out)
        )
        return left_out

    shutil.copytree(source, destination, symlinks=True, ignore=leave_out_junctions)
    for relative, target in junctions:
        link = destination / relative
        # ``os.readlink`` spells the target with the ``\\?\`` prefix, which
        # CreateJunction would store verbatim. A target without a drive letter,
        # such as a volume mounted on a folder, cannot be recreated: the move fails.
        plain = target.removeprefix("\\\\?\\")
        drive, root, _ = os.path.splitroot(plain)
        if len(drive) != 2 or drive[1] != ":" or not root:
            raise OSError(errno.EINVAL, f"Cannot recreate the junction to {target}", str(link))
        _winapi.CreateJunction(plain, str(link))


def _discard_tree(path: Path, what: str) -> None:
    try:
        _remove_tree(path)
    except OSError as error:
        _LOGGER.warning("Could not remove the %s at %s: %s", what, path, error)


def remove_tree(path: Path, *, within: Path, stop: Callable[[], None] | None = None) -> None:
    """Delete the file, directory tree or link at ``path``, which must lie inside ``within``.

    The containment check resolves the parent directories but not ``path`` itself,
    so a ``path`` that is a symbolic link or junction is removed as a link entry and
    whatever it points at stays untouched; links inside a tree are never followed
    either. Read-only attributes, such as those of ``.git`` objects, are cleared. A
    missing ``path`` counts as removed. A ``path`` outside ``within``, or ``within``
    itself, raises ``ValueError`` before anything is deleted.

    ``stop`` is called before each entry of a tree is deleted; whatever it raises
    ends the removal there and propagates. What is not deleted yet stays, and a
    later removal of the same ``path`` finishes it.
    """
    root = Path(within).resolve()
    target = Path(path).parent.resolve() / Path(path).name
    if target == root or not target.is_relative_to(root) or Path(path).name in ("", ".", ".."):
        raise ValueError(f"refusing to remove {path}: it is not inside {within}")
    try:
        status = os.lstat(target)
    except FileNotFoundError:
        return
    if _is_link(status) or not stat.S_ISDIR(status.st_mode):
        try:
            os.unlink(target)
        except PermissionError:
            _make_writable(target)
            os.unlink(target)
        return
    if stop is None:
        _remove_tree(target)
    else:
        _remove_tree_stoppable(target, stop)


def _remove_tree_stoppable(path: Path, stop: Callable[[], None]) -> None:
    """Delete a directory tree bottom-up, calling ``stop`` before each entry.

    Links and junctions inside it are removed as entries, never followed.
    """
    with os.scandir(path) as entries:
        children = [Path(entry.path) for entry in entries]
    for child in children:
        stop()
        status = os.lstat(child)
        if _is_link(status) or not stat.S_ISDIR(status.st_mode):
            _retry_writable(os.unlink, child)
        else:
            _remove_tree_stoppable(child, stop)
    stop()
    _retry_writable(os.rmdir, path)


def _retry_writable(action: Callable[[Path], object], path: Path) -> None:
    try:
        action(path)
    except PermissionError:
        _make_writable(path)
        action(path)


def _remove_tree(path: Path) -> None:
    """Delete a tree, clearing read-only attributes such as those of ``.git`` objects."""
    shutil.rmtree(path, onexc=_clear_read_only_and_retry)


def _clear_read_only_and_retry(action: Callable[[str], object], path: str, _error: object) -> None:
    _make_writable(path)
    action(path)


def _is_link(status: os.stat_result) -> bool:
    """Whether an ``lstat`` result is a symbolic link or a Windows junction."""
    if sys.platform == "win32" and status.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT:
        return True
    return stat.S_ISLNK(status.st_mode)


def _make_writable(path: Path | str) -> None:
    """Clear the read-only attribute of ``path`` itself, never of what a link points at.

    Where ``os.chmod`` cannot leave links unfollowed (Linux), a link is left as it
    is: its removal then fails instead of changing a directory outside the tree.
    """
    status = os.lstat(path)
    if not _is_link(status):
        os.chmod(path, status.st_mode | stat.S_IWRITE)
    elif os.chmod in os.supports_follow_symlinks:
        os.chmod(path, status.st_mode | stat.S_IWRITE, follow_symlinks=False)
