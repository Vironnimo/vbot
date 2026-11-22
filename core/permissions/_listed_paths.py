"""Inspect the links in a filesystem listing whose names a shell command consumes."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.permissions._paths import path_key, reaches
from core.utils.file_status import is_dir_strict, is_file_strict

if TYPE_CHECKING:
    from core.permissions._refusal import PermissionRefusal
    from core.permissions.commands.commands import PlannedPathAccess
    from core.permissions.permissions import PermissionBoundary


def concrete_listing_access(
    boundary: PermissionBoundary, access: PlannedPathAccess
) -> PlannedPathAccess:
    """An existing regular file can only be read as a single entry.

    Verify its type from metadata without opening it or enumerating its parent.
    Keep the wider scope on stat failure. This runs again on approval rechecks,
    so a directory and a file at the same path have different grant identities.
    """
    if access.kind == "read" and access.enumerated and access.subtree and access.glob is None:
        direct = boundary.check_path(access.path, "read")
        if direct is None or direct.reason == "not_permitted":
            try:
                if is_file_strict(access.path):
                    return replace(access, subtree=False, enumerated=False)
            except OSError:
                pass
    return access


def check_listed_links(
    boundary: PermissionBoundary, access: PlannedPathAccess, *, windows: bool
) -> Iterator[tuple[Path, bool, PermissionRefusal | None]]:
    """Verify every link the consumer can reach, including links in a linked directory.

    The Boundary checks each alias and its resolved subtree before this walk inspects
    a target directory. A partial glob cannot stand in for the whole tree.
    Concrete link identities are returned for approval rechecks.
    """
    scopes = [(access.path, access.glob, access.subtree)]
    whole_roots: set[Path] = set()
    while scopes:
        root, glob, subtree = scopes.pop()
        resolved = root.resolve()
        if resolved in whole_roots or not whole_roots.isdisjoint(resolved.parents):
            continue
        if subtree and glob in (None, "*"):
            whole_roots.add(resolved)
        for link, deeper in sorted(
            _listed_links(boundary, root, glob=glob, subtree=subtree, windows=windows)
        ):
            refusal = boundary.check_path(link, access.kind, subtree=deeper)
            if deeper and refusal is not None and refusal.reason == "not_permitted":
                raise PermissionError  # Its contents cannot be checked under this boundary.
            yield link, deeper, refusal
            if refusal is not None:
                if refusal.reason != "not_permitted":
                    return
                continue
            if deeper and is_dir_strict(link):
                # A directory supplied as a concrete operand can be read recursively.
                # Check its own tree as well, without recursing through the link name.
                scopes.append((link.resolve(strict=True), None, True))


def _listed_links(
    boundary: PermissionBoundary, root: Path, *, glob: str | None, subtree: bool, windows: bool
) -> Iterator[tuple[Path, bool]]:
    """Yield matching links and whether the consumer could use entries below them.

    Read directory entries only. Never follow a discovered link while walking.
    A missing enumeration root has no entries at preflight. Failure to inspect
    an existing directory propagates, so the caller refuses the command.
    """
    key = path_key(root.as_posix(), windows=windows)
    reach = tuple(path_key(glob, windows=windows).split("/")) if glob else ()
    if subtree:
        reach += ("**",)
    whole_tree = reach in (("**",), ("*", "**"))
    one_level = reach == ("*",)
    pending = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        if boundary.check_path(directory, "read") is not None:
            raise PermissionError  # Never enumerate a folder the current boundary cannot read.
        try:
            entries = os.scandir(directory)
        except FileNotFoundError, NotADirectoryError:
            continue
        with entries:
            for entry in entries:
                if not (whole_tree or one_level) and not reaches(
                    key, reach, path_key(Path(entry.path).as_posix(), windows=windows)
                ):
                    continue
                deeper = "**" in reach or depth + 1 < len(reach)
                if entry.is_symlink() or entry.is_junction():
                    yield Path(entry.path), deeper
                elif deeper and entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), depth + 1))
