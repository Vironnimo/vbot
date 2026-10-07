"""One directory of the server's filesystem at a time, for path pickers.

Accessors may run on another host than the server, so every path a user picks on
the server is browsed here: the "places" to start from (filesystem roots and the
home directory), or the entries of one directory. Listings are lazy (one directory
per call), bounded (``DIRECTORY_LISTING_LIMIT`` entries) and timed: a directory
that does not answer within ``DIRECTORY_LISTING_TIMEOUT_SECONDS``, such as one on
an unresponsive network share, fails with the reason ``timeout`` instead of
holding the caller.

An optional ``root`` confines a listing to one directory tree: paths are relative
to it, ``..`` cannot climb out of it, and a link or junction that resolves outside
it is refused. Paths in results use ``/`` separators.

A directory that exists but cannot be read is ``unreadable``, never ``not_found``.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Literal

from core.utils.errors import VBotError
from core.utils.file_status import is_link_status, stat_or_none
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("utils.directory_listing")

# Entries one listing returns at most; a larger directory is marked truncated.
DIRECTORY_LISTING_LIMIT = 5000

# Wall-clock budget for one listing, from the request until the result.
DIRECTORY_LISTING_TIMEOUT_SECONDS = 3.0

# Windows error for a path whose syntax cannot name an entry (``C:/a:b``).
_ERROR_INVALID_NAME = 123

type EntryKind = Literal["directory", "file"]
type FailureReason = Literal["not_found", "not_a_directory", "unreadable", "timeout"]

# A listing that does not answer in time keeps its worker until the filesystem
# answers; the worker only reads, so nobody waits for it.
_POOL = BoundedWorkerPool(name="directory-listing", max_workers=4)
_abandoned: set[asyncio.Task[DirectoryListing]] = set()

# Clock seam: tests move it to expire a listing's budget without waiting.
_monotonic = time.monotonic


@dataclass(frozen=True)
class DirectoryEntry:
    """One entry of a listed directory."""

    name: str
    kind: EntryKind
    # A symbolic link or Windows junction; it is listed and can be entered.
    link: bool
    # A dot-name, or an entry with the Windows hidden attribute.
    hidden: bool


@dataclass(frozen=True)
class DirectoryListing:
    """The entries of one directory, or the places listing.

    ``path`` is the listed directory: absolute with ``/`` separators, or relative to
    the listing's root (``""`` is the root itself); the places listing has ``""``.
    ``parent`` is the parent in the same form, ``None`` at a filesystem root, at the
    listing's root and for the places listing. ``home`` is set only for the places
    listing. ``separator`` is the server's native path separator.
    """

    path: str
    parent: str | None
    entries: tuple[DirectoryEntry, ...]
    truncated: bool
    separator: str
    home: str | None = None


class DirectoryListingError(VBotError):
    """A directory could not be listed; ``reason`` says why."""

    def __init__(self, reason: FailureReason, message: str) -> None:
        super().__init__(message)
        self.reason: FailureReason = reason


class ListingPathError(VBotError):
    """The requested path is not one a listing may name: relative without a root,
    or outside the listing's root."""


async def list_directory(
    path: str | None, *, root: str | None = None, include_files: bool = False
) -> DirectoryListing:
    """List ``path`` off the Event Loop within the listing budget.

    ``path`` ``None`` lists the places (filesystem roots, plus ``home``); with
    ``root`` it lists the root. Otherwise ``path`` is absolute, or ``~`` / ``~/...``,
    and with ``root`` relative to it (``""`` is the root). ``include_files=False``
    lists only directories.

    Raises :class:`ListingPathError` for a path the request may not name and
    :class:`DirectoryListingError` when the directory cannot be listed, also with
    the reason ``timeout`` when the filesystem does not answer within the budget;
    the listing then finishes in the background without a receiver.
    """
    timeout_seconds = DIRECTORY_LISTING_TIMEOUT_SECONDS
    deadline = _monotonic() + timeout_seconds
    task = asyncio.ensure_future(
        _POOL.run(_list, path, root=root, include_files=include_files, deadline=deadline)
    )
    try:
        done, _pending = await asyncio.wait((task,), timeout=timeout_seconds)
    except asyncio.CancelledError:
        _abandon(task)
        raise
    if not done:
        _abandon(task)
        raise _timeout(path or root or "The filesystem")
    return task.result()


def list_directory_sync(
    path: str | None,
    *,
    root: str | None = None,
    include_files: bool = False,
    timeout_seconds: float | None = None,
) -> DirectoryListing:
    """List ``path`` like :func:`list_directory`, blocking the calling thread.

    The budget is checked between entries, so a directory whose first read does
    not return holds the caller; :func:`list_directory` does not wait for it.
    """
    budget = DIRECTORY_LISTING_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    return _list(path, root=root, include_files=include_files, deadline=_monotonic() + budget)


def _list(
    path: str | None, *, root: str | None, include_files: bool, deadline: float
) -> DirectoryListing:
    if "\x00" in (path or "") or "\x00" in (root or ""):
        raise ListingPathError("A path cannot contain a NUL character.")
    if root is None and path is None:
        return _places()
    if root is None:
        assert path is not None
        absolute = _absolute(path)
        parent = os.path.dirname(absolute)
        shown = PurePath(absolute).as_posix()
        entries, truncated = _read(absolute, shown, include_files=include_files, deadline=deadline)
        return DirectoryListing(
            path=shown,
            parent=None if parent == absolute else PurePath(parent).as_posix(),
            entries=entries,
            truncated=truncated,
            separator=os.sep,
        )
    base = _root(root)
    parts = _relative_parts(path or "")
    target = base.joinpath(*parts)
    resolved = target.resolve()
    if resolved != base and base not in resolved.parents:
        raise ListingPathError(f'"{"/".join(parts)}" leads outside the listing root.')
    relative = "/".join(parts)
    entries, truncated = _read(
        str(target),
        relative or "The listing root",
        include_files=include_files,
        deadline=deadline,
    )
    return DirectoryListing(
        path=relative,
        parent="/".join(parts[:-1]) if parts else None,
        entries=entries,
        truncated=truncated,
        separator=os.sep,
    )


def _places() -> DirectoryListing:
    if sys.platform == "win32":
        roots = sorted(PurePath(drive).as_posix() for drive in os.listdrives())
    else:
        roots = ["/"]
    return DirectoryListing(
        path="",
        parent=None,
        entries=tuple(
            DirectoryEntry(name=name, kind="directory", link=False, hidden=False) for name in roots
        ),
        truncated=False,
        separator=os.sep,
        home=Path.home().as_posix(),
    )


def _expand_home(path: str) -> str:
    """Expand ``~`` and ``~/...``; ``~user`` forms stay as they are."""
    separators = ("/", os.sep)
    if path == "~" or path.startswith(tuple(f"~{separator}" for separator in separators)):
        return os.path.expanduser(path)
    return path


def _absolute(path: str) -> str:
    expanded = _expand_home(path)
    if not os.path.isabs(expanded):
        raise ListingPathError(f'"{path}" is not an absolute path.')
    return os.path.normpath(expanded)


def _root(root: str) -> Path:
    expanded = _expand_home(root)
    if not os.path.isabs(expanded):
        raise ListingPathError(f'The listing root "{root}" is not an absolute path.')
    return Path(expanded).resolve()


def _relative_parts(path: str) -> list[str]:
    """Split a root-relative path into names, resolving ``.`` and ``..`` lexically."""
    if PurePath(path).anchor:
        raise ListingPathError(f'"{path}" must be relative to the listing root.')
    separators = "/\\" if os.sep == "\\" else "/"
    parts: list[str] = []
    for name in _split(path, separators):
        if name in {"", "."}:
            continue
        if name == "..":
            if not parts:
                raise ListingPathError(f'"{path}" leads outside the listing root.')
            parts.pop()
            continue
        parts.append(name)
    return parts


def _split(path: str, separators: str) -> list[str]:
    names = [path]
    for separator in separators:
        names = [part for name in names for part in name.split(separator)]
    return names


def _read(
    directory: str, shown: str, *, include_files: bool, deadline: float
) -> tuple[tuple[DirectoryEntry, ...], bool]:
    _check_deadline(deadline, shown)
    entries: list[DirectoryEntry] = []
    truncated = False
    try:
        with os.scandir(directory) as listing:
            for entry in listing:
                _check_deadline(deadline, shown)
                described = _describe(entry)
                if described is None or (described.kind == "file" and not include_files):
                    continue
                if len(entries) >= DIRECTORY_LISTING_LIMIT:
                    truncated = True
                    break
                entries.append(described)
    except DirectoryListingError:
        raise
    except OSError as error:
        raise _failure(directory, shown, error) from error
    entries.sort(key=lambda item: (item.kind != "directory", item.name.casefold(), item.name))
    return tuple(entries), truncated


def _describe(entry: os.DirEntry[str]) -> DirectoryEntry | None:
    try:
        status = entry.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None  # Removed while the directory was being read.
    except OSError:
        status = None
    try:
        directory = entry.is_dir()
    except OSError:
        directory = False
    hidden = entry.name.startswith(".")
    if sys.platform == "win32" and status is not None:
        hidden = hidden or bool(status.st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN)
    return DirectoryEntry(
        name=entry.name,
        kind="directory" if directory else "file",
        link=status is not None and is_link_status(status),
        hidden=hidden,
    )


def _failure(directory: str, shown: str, error: OSError) -> DirectoryListingError:
    if isinstance(error, NotADirectoryError):
        try:
            status = stat_or_none(directory)
        except OSError:
            status = None
        if status is not None and not stat.S_ISDIR(status.st_mode):
            return DirectoryListingError("not_a_directory", f"{shown} is not a directory.")
        return DirectoryListingError("not_found", f"No such directory: {shown}")
    if isinstance(error, FileNotFoundError) or (
        getattr(error, "winerror", None) == _ERROR_INVALID_NAME
    ):
        return DirectoryListingError("not_found", f"No such directory: {shown}")
    # The operating system's own text may be localized; the reason is what counts.
    detail = ": access is denied" if isinstance(error, PermissionError) else ""
    return DirectoryListingError("unreadable", f"{shown} cannot be read{detail}.")


def _check_deadline(deadline: float, shown: str) -> None:
    if _monotonic() > deadline:
        raise _timeout(shown)


def _timeout(shown: str) -> DirectoryListingError:
    return DirectoryListingError("timeout", f"{shown} did not answer in time; try again later.")


def _abandon(task: asyncio.Task[DirectoryListing]) -> None:
    _abandoned.add(task)
    task.add_done_callback(_settle_abandoned)


def _settle_abandoned(task: asyncio.Task[DirectoryListing]) -> None:
    _abandoned.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None and not isinstance(error, (DirectoryListingError, ListingPathError)):
        _LOGGER.error("Directory listing failed after its caller stopped waiting", exc_info=error)


__all__ = [
    "DIRECTORY_LISTING_LIMIT",
    "DIRECTORY_LISTING_TIMEOUT_SECONDS",
    "DirectoryEntry",
    "DirectoryListing",
    "DirectoryListingError",
    "ListingPathError",
    "list_directory",
    "list_directory_sync",
]
