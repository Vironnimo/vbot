"""Ranked suggestions for a requested path that does not exist."""

from __future__ import annotations

import json
import os
from difflib import SequenceMatcher
from itertools import islice
from pathlib import Path
from typing import Literal

from core.tools.model_names import model_tool_name
from core.tools.search import display_search_path

_SCAN_LIMIT = 200
_RESULT_LIMIT = 5
_MIN_RATIO = 0.55
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

    name_ratio = SequenceMatcher(None, requested_name, candidate_name).ratio()
    stem_ratio = SequenceMatcher(None, requested_stem, candidate_stem).ratio()
    ratio = max(name_ratio, stem_ratio)
    if requested_name.startswith(candidate_name) or candidate_name.startswith(requested_name):
        return 3, ratio
    if requested_name in candidate_name or candidate_name in requested_name:
        return 2, ratio
    if ratio >= _MIN_RATIO:
        return 1, ratio
    return None


def similar_entries(
    missing: Path, *, kind: EntryKind = "any", limit: int = _RESULT_LIMIT
) -> list[Path]:
    """Return existing entries beside a missing path, best match first."""
    candidates: list[tuple[tuple[int, float], str, Path]] = []
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
            if score is not None:
                candidates.append((score, entry.name.casefold(), entry))
    except OSError:
        return []
    candidates.sort(key=lambda item: (-item[0][0], -item[0][1], item[1]))
    return [entry for _, _, entry in candidates[:limit]]


def corrected_paths(missing: Path, cwd: Path) -> list[Path]:
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
        if repeated == tail[-count:] and candidate.exists():
            suggestions.append(candidate)
    repaired = _repair_components(missing)
    if repaired is not None:
        suggestions.append(repaired)
    # Only a missing name in an existing folder is looked for elsewhere; a
    # missing folder is a misspelling or no path at all (example.com/cat.png).
    if relative and missing.parent.is_dir():
        suggestions.extend(_same_name_below(cwd, missing.name))
    suggestions.extend(similar_entries(missing))
    return list(dict.fromkeys(suggestions))[:_RESULT_LIMIT]


def _same_name_below(root: Path, name: str) -> list[Path]:
    """Return entries named ``name`` below ``root``, shallow levels first.

    Hidden directories, dependency folders and levels past the bound are not
    searched; the walk stops after a bounded number of entries.
    """
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
                    entries = sorted(
                        islice(listing, _TREE_SCAN_LIMIT - scanned),
                        key=lambda entry: entry.name.casefold(),
                    )
            except OSError:
                continue
            scanned += len(entries)
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


def _repair_components(missing: Path) -> Path | None:
    """Replace each missing component with its closest existing sibling."""
    names: list[str] = []
    current = missing
    while not current.exists():
        if current.parent == current:
            return None
        names.append(current.name)
        current = current.parent
    if len(names) < 2:
        # A missing final name alone is covered by its similar siblings.
        return None
    for position, name in enumerate(reversed(names)):
        kind: EntryKind = "any" if position == len(names) - 1 else "dirs"
        matches = similar_entries(current / name, kind=kind, limit=1)
        if not matches:
            return None
        current = matches[0]
    return current


def missing_file_message(missing: Path, cwd: Path) -> str:
    """Build a not-found error that names paths the next call can use."""
    label = display_search_path(missing, cwd=cwd)
    suggestions = corrected_paths(missing, cwd)
    if suggestions:
        similar = ", ".join(display_search_path(candidate, cwd=cwd) for candidate in suggestions)
        return f"File not found: {label} (similar: {similar})."
    parent = missing.parent
    shown = display_search_path(parent, cwd=cwd)
    if parent.is_dir():
        call = f"{model_tool_name('read')}(path={json.dumps(shown, ensure_ascii=False)})"
        return f"File not found: {label}. {call} lists its directory."
    return f"File not found: {label}. Its directory {shown} does not exist."
