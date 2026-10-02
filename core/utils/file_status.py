"""What a filesystem entry is, without mistaking "cannot tell" for "missing".

Since Python 3.14, ``Path.exists``, ``Path.is_dir`` and ``Path.is_file`` behave like
``os.path.exists`` always did: when the check itself fails - a permission or I/O
error, a network share or cloud-file provider that does not answer - they return
``False``, so an entry that cannot be checked reads as missing. Where that answer
decides about data (seeding, overwriting, deleting, restoring, adopting), use the
checks here instead: they report an entry that does not exist as missing and raise
the ``OSError`` otherwise, so the caller can treat it as unavailable.
"""

from __future__ import annotations

import os
import stat

# What an entry that does not exist raises: no such file, a parent that is a file,
# or a path that cannot exist at all (an embedded NUL character).
_MISSING = (FileNotFoundError, NotADirectoryError, ValueError)


def stat_or_none(
    path: str | os.PathLike[str], *, follow_symlinks: bool = True
) -> os.stat_result | None:
    """The status of ``path``, or ``None`` when nothing exists there.

    Raises ``OSError`` when the entry may exist but cannot be checked. With
    ``follow_symlinks=False`` a link is reported itself, a dangling one included.
    """

    try:
        return os.stat(path, follow_symlinks=follow_symlinks)
    except _MISSING:
        return None


def exists_strict(path: str | os.PathLike[str], *, follow_symlinks: bool = True) -> bool:
    """Whether ``path`` exists; raises ``OSError`` when that cannot be told."""

    return stat_or_none(path, follow_symlinks=follow_symlinks) is not None


def is_dir_strict(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` is a directory or a link to one; raises ``OSError`` when unknown."""

    status = stat_or_none(path)
    return status is not None and stat.S_ISDIR(status.st_mode)


def is_file_strict(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` is a regular file or a link to one; raises ``OSError`` when unknown."""

    status = stat_or_none(path)
    return status is not None and stat.S_ISREG(status.st_mode)


__all__ = [
    "exists_strict",
    "is_dir_strict",
    "is_file_strict",
    "stat_or_none",
]
