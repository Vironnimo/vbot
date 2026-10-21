"""Keep a restricted Session's file search inside what its Permission Rules let it read.

search_files checks each root before touching it (``_search_roots``). This
module confines the rest: flags that read other paths, excluding globs for
what the rules refuse below the roots of each ripgrep run, and the check every
reported path passes. ripgrep follows no links here, so below a readable root
only deny rules and protected locations can refuse a file
(``PermissionBoundary.refusable_below``). Full access changes nothing.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from core.permissions import PermissionBoundary, PermissionDenied
from core.tools._search_query import SearchQuery

if TYPE_CHECKING:
    # Annotations only: the execution owner applies these confinements.
    from core.tools._search_execution import Scope

LINKS_NOTE = (
    "-L (--follow) was ignored: under the permission rules, search_files does not follow links."
)
# Flags whose value is a file ripgrep reads.
_PATH_FLAGS = ("--file", "--ignore-file")
# Rule patterns take these characters literally; ripgrep globs do so inside a class.
_GLOB_LITERAL = re.compile(r"[\[\]{}]")


@dataclass(frozen=True)
class Confinement:
    """What one ripgrep run of a restricted Session leaves out.

    ``globs`` are excluding glob arguments placed after every other glob;
    ``filtered`` says that the rules can refuse something below the run's
    roots, so each path the run reports is checked as well.
    """

    globs: list[str] = field(default_factory=list)
    filtered: bool = False


UNCONFINED = Confinement()


def confine_flags(query: SearchQuery, cwd: Path, permissions: PermissionBoundary) -> None:
    """Drop link following and check the files that path flags name, before any run.

    ``--file`` and ``--ignore-file`` values resolve against ``cwd`` and pass
    as absolute paths, since each ripgrep run starts in its own scope.
    """
    if not permissions.restricted:
        return
    kept: list[str] = []
    for argument in query.rg_args:
        if argument == "--follow":
            if LINKS_NOTE not in query.notes:
                query.notes.append(LINKS_NOTE)
            continue
        name, equals, value = argument.partition("=")
        if equals and name in _PATH_FLAGS:
            path = Path(os.path.abspath(cwd / Path(value).expanduser()))
            refusal = permissions.check_path(path, "read")
            if refusal is not None:
                raise PermissionDenied(refusal)
            argument = f"{name}={path}"
        kept.append(argument)
    query.rg_args = kept


def scope_confinement(scope: Scope, permissions: PermissionBoundary) -> Confinement:
    """The excluding globs and the filter one ripgrep run needs.

    The excluding globs keep refused files out of ripgrep's searched-file
    count, so they match without case on Windows, as the rules do there.
    """
    if not permissions.restricted:
        return UNCONFINED
    context = permissions.context
    windows = (context.platform if context else sys.platform) == "win32"
    flag = "--iglob" if windows else "--glob"
    globs: list[str] = []
    filtered = False
    for path in scope.paths:
        root = Path(os.path.normpath(scope.cwd / path))
        relative = path.replace("\\", "/").strip("/")
        prefix = "" if relative in {"", "."} else relative + "/"
        for pattern in permissions.refusable_below(root, "read"):
            filtered = True
            glob = _GLOB_LITERAL.sub(lambda match: f"[{match.group()}]", f"!/{prefix}{pattern}")
            # ripgrep reads a backslash as an escape; only the filter leaves such paths out.
            if pattern != "**" and "\\" not in glob:
                globs.append(f"{flag}={glob}")
    return Confinement(list(dict.fromkeys(globs)), filtered)


def permitted(permissions: PermissionBoundary, scope: Scope, reported: str | bytes) -> bool:
    """Whether a path a run reported, relative to its scope, may be read."""
    name = os.fsdecode(reported)
    return permissions.check_path(os.path.normpath(scope.cwd / name), "read") is None
