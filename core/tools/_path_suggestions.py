"""Ranked suggestions for a requested path that does not exist.

Every function takes the Run's ``PermissionBoundary``: a restricted Session gets
only entries it may read, and directories it may not read are never listed or
checked, so a suggestion never reveals what lies outside its readable area. A
refused entry of a readable folder whose name is close is named as refused
(``refused_note``), as a listing of that folder names it.
"""

from __future__ import annotations

import json
import os
from difflib import SequenceMatcher
from itertools import islice
from pathlib import Path
from typing import Literal

from core.permissions import PermissionBoundary
from core.tools._refused_notes import similar_note
from core.tools.model_names import model_tool_name
from core.tools.search import display_search_path
from core.utils.edit_distance import edit_distance

_SCAN_LIMIT = 200
_RESULT_LIMIT = 5
# A similar name may differ by one typo per this many characters of the missing
# name's stem, and always by one.
_CHARACTERS_PER_TYPO = 4
# The same name elsewhere is searched below the working directory, shallow levels
# first, within this many directory entries and levels.
_TREE_SCAN_LIMIT = 5000
_TREE_DEPTH = 6
_SKIPPED = frozenset({"node_modules", "__pycache__"})

EntryKind = Literal["any", "files", "dirs"]


def _score(requested: Path, candidate: Path) -> tuple[int, float] | None:
    """Rank a name by similarity to a missing name."""
    requested_name = requested.name.casefold()
    candidate_name = candidate.name.casefold()
    requested_stem = requested.stem.casefold()
    candidate_stem = candidate.stem.casefold()

    if candidate_name == requested_name:
        return 5, 1.0
    if candidate_stem == requested_stem:
        return 4, 1.0

    stem_ratio = SequenceMatcher(None, requested_stem, candidate_stem).ratio()
    pairs = [(requested_stem, candidate_stem)]
    if requested.suffix and requested.suffix.casefold() == candidate.suffix.casefold():
        # A shared extension says nothing about which file was meant: parser.py
        # is not like ast.py.
        ratio = stem_ratio
    else:
        name_ratio = SequenceMatcher(None, requested_name, candidate_name).ratio()
        ratio = max(name_ratio, stem_ratio)
        pairs.append((requested_name, candidate_name))
    if requested_name.startswith(candidate_name) or candidate_name.startswith(requested_name):
        return 3, ratio
    if requested_name in candidate_name or candidate_name in requested_name:
        return 2, ratio
    # Any other name is close only when it differs by a few typos. A shared beginning
    # alone makes names look alike: test_expr.py suggested test_index.py,
    # test_api.py, test_errors.py, test_planner.py and test_shell.py.
    typos = max(1, len(requested_stem) // _CHARACTERS_PER_TYPO)
    if any(edit_distance(first, second, typos) <= typos for first, second in pairs):
        return 1, ratio
    return None


def _readable(permissions: PermissionBoundary, path: Path) -> bool:
    return permissions.check_path(path, "read") is None


def similar_entries(
    missing: Path,
    *,
    permissions: PermissionBoundary,
    kind: EntryKind = "any",
    limit: int = _RESULT_LIMIT,
) -> list[Path]:
    """Return existing entries beside a missing path, best match first."""
    candidates: list[tuple[tuple[int, float], str, Path]] = []
    if not _readable(permissions, missing.parent):
        return []
    try:
        for entry in islice(missing.parent.iterdir(), _SCAN_LIMIT):
            try:
                if (kind == "files" and not entry.is_file()) or (
                    kind == "dirs" and not entry.is_dir()
                ):
                    continue
            except OSError:
                continue
            score = _score(missing, entry)
            if score is not None and _readable(permissions, entry):
                candidates.append((score, entry.name.casefold(), entry))
    except OSError:
        return []
    candidates.sort(key=lambda item: (-item[0][0], -item[0][1], item[1]))
    return [entry for _, _, entry in candidates[:limit]]


def refused_similar(missing: Path, permissions: PermissionBoundary) -> list[Path]:
    """Entries the rules refuse to read whose names are close to the first missing part.

    Only entries of the deepest existing folder, and only when the rules let
    the Session read that folder: their names are what a listing of it names
    too, never what lies inside a refused folder. Best match first.
    """
    if not permissions.restricted:
        return []
    first = missing
    while first.parent != first and not first.parent.exists():
        first = first.parent
    folder = first.parent
    if folder == first or not _readable(permissions, folder):
        return []
    try:
        with os.scandir(folder) as listing:
            scan = list(islice(listing, _SCAN_LIMIT))
    except OSError:
        return []
    kept = {entry.path for entry in permissions.readable_entries(folder, scan)}
    candidates: list[tuple[tuple[int, float], str, Path]] = []
    for entry in scan:
        if entry.path in kept:
            continue
        score = _score(first, Path(entry.path))
        if score is not None:
            candidates.append((score, entry.name.casefold(), Path(entry.path)))
    candidates.sort(key=lambda item: (-item[0][0], -item[0][1], item[1]))
    return [path for _, _, path in candidates[:_RESULT_LIMIT]]


def corrected_paths(missing: Path, cwd: Path, *, permissions: PermissionBoundary) -> list[Path]:
    """Return existing paths that a missing absolute path most likely means.

    Covers a relative path that repeats the end of the working directory (such
    as project/src from inside project), a misspelled name at any level, for a
    name missing from an existing directory the same name in another directory
    below the working directory (corpus.py for tests/corpus.py), and similar
    names beside the missing one.
    """
    suggestions: list[Path] = []
    try:
        relative = missing.relative_to(cwd).parts
    except ValueError:
        relative = ()
    tail = [part.casefold() for part in cwd.parts]
    for count in range(1, len(relative)):
        repeated = [part.casefold() for part in relative[:count]]
        candidate = cwd.joinpath(*relative[count:])
        if repeated == tail[-count:] and _readable(permissions, candidate) and candidate.exists():
            suggestions.append(candidate)
    repaired = _repair_components(missing, permissions)
    if repaired is not None:
        suggestions.append(repaired)
    # Only a missing name in an existing folder is looked for elsewhere; a
    # missing folder is a misspelling or no path at all (example.com/cat.png).
    if relative and _readable(permissions, missing.parent) and missing.parent.is_dir():
        suggestions.extend(_same_name_below(cwd, missing.name, permissions))
    suggestions.extend(similar_entries(missing, permissions=permissions))
    return list(dict.fromkeys(suggestions))[:_RESULT_LIMIT]


def _same_name_below(root: Path, name: str, permissions: PermissionBoundary) -> list[Path]:
    """Return entries named ``name`` below ``root``, shallow levels first.

    Hidden directories, dependency folders and levels past the bound are not
    searched; the walk stops after a bounded number of entries. A restricted
    Session's walk sees only entries it may read.
    """
    if not _readable(permissions, root):
        return []
    wanted = name.casefold()
    found: list[Path] = []
    level = [root]
    scanned = 0
    for _ in range(_TREE_DEPTH):
        below: list[Path] = []
        for directory in level:
            if scanned >= _TREE_SCAN_LIMIT:
                return found
            try:
                with os.scandir(directory) as listing:
                    scan = list(islice(listing, _TREE_SCAN_LIMIT - scanned))
            except OSError:
                continue
            scanned += len(scan)
            entries = sorted(
                permissions.readable_entries(directory, scan),
                key=lambda entry: entry.name.casefold(),
            )
            for entry in entries:
                if entry.name.casefold() == wanted:
                    found.append(Path(entry.path))
                    if len(found) == _RESULT_LIMIT:
                        return found
                try:
                    walk = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if walk and not entry.name.startswith(".") and entry.name not in _SKIPPED:
                    below.append(Path(entry.path))
        level = below
    return found


def _repair_components(missing: Path, permissions: PermissionBoundary) -> Path | None:
    """Replace each missing component with its closest existing sibling."""
    names: list[str] = []
    current = missing
    while not current.exists():
        if current.parent == current or not _readable(permissions, current.parent):
            return None
        names.append(current.name)
        current = current.parent
    if len(names) < 2:
        # A missing final name alone is covered by its similar siblings.
        return None
    for position, name in enumerate(reversed(names)):
        kind: EntryKind = "any" if position == len(names) - 1 else "dirs"
        matches = similar_entries(current / name, permissions=permissions, kind=kind, limit=1)
        if not matches:
            return None
        current = matches[0]
    return current


def missing_file_message(
    missing: Path, cwd: Path, *, permissions: PermissionBoundary, elsewhere: str | None = None
) -> str:
    """Build a not-found error that names paths the next call can use.

    ``elsewhere`` is a sentence naming another Tool's call for what the path names
    instead, such as a Swarm Wiki page (``_swarm_references.wiki_page_call``).
    """
    label = display_search_path(missing, cwd=cwd)
    suggestions = corrected_paths(missing, cwd, permissions=permissions)
    refused = refused_note(missing, cwd, permissions, also=bool(suggestions))
    if suggestions:
        similar = ", ".join(display_search_path(candidate, cwd=cwd) for candidate in suggestions)
        message = f"File not found: {label} (similar: {similar}).{refused}"
        return f"{message} {elsewhere}" if elsewhere else message
    if elsewhere:
        return f"{label} is not a file.{refused} {elsewhere}"
    parent = missing.parent
    shown = display_search_path(parent, cwd=cwd)
    if not _readable(permissions, parent):
        return f"File not found: {label}."
    if parent.is_dir():
        call = f"{model_tool_name('read')}(path={json.dumps(shown, ensure_ascii=False)})"
        return f"File not found: {label}.{refused} {call} lists its directory."
    return f"File not found: {label}. Its directory {shown} does not exist.{refused}"


def refused_note(missing: Path, cwd: Path, permissions: PermissionBoundary, *, also: bool) -> str:
    """The sentence naming refused entries similar to ``missing``, with a leading space, or ""."""
    refused = refused_similar(missing, permissions)
    if not refused:
        return ""
    return " " + similar_note([display_search_path(path, cwd=cwd) for path in refused], also=also)
