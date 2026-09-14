"""Shared internal helpers for file-search tools."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pathspec import PathSpec
from pathspec.pattern import Pattern

from core.utils.paths import model_path

if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.tools.tools import ToolContext

SEARCH_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 50 * 1024


class SearchBudget:
    """Cooperative stop signal for one search tool call.

    Search handlers run in a worker thread, so nothing external can interrupt
    a long filesystem walk: the loops must poll. ``keep_going()`` folds the
    three stop reasons (user cancel, run cancel, wall-clock timeout) into one
    check and records which one fired, so the handler can decide between a
    cancel failure envelope and partial results with a timeout marker.

    ``context=None`` builds a timeout-only budget for walks that run outside a
    tool call (e.g. the ``files.list`` RPC listing) — no cancel signals exist
    there, so only the wall clock stops the walk.
    """

    def __init__(self, context: ToolContext | None, timeout_seconds: float | None = None) -> None:
        # Resolved at call time (not import time) so tests can shrink the
        # module-level timeout via monkeypatch.
        if timeout_seconds is None:
            timeout_seconds = SEARCH_TIMEOUT_SECONDS
        self._context = context
        self._deadline = time.monotonic() + timeout_seconds
        self.timed_out = False
        self.cancelled_by_user = False
        self.run_cancelled = False

    def remaining_seconds(self) -> float:
        """Return the wall-clock budget left, floored at zero."""
        return max(self._deadline - time.monotonic(), 0.0)

    def keep_going(self) -> bool:
        """Poll all stop conditions; record and return False on the first hit."""
        if self._context is not None:
            if self._context.was_cancelled_by_user():
                self.cancelled_by_user = True
                return False
            if self._context.is_cancelled():
                self.run_cancelled = True
                return False
        if time.monotonic() > self._deadline:
            self.timed_out = True
            return False
        return True

    @property
    def stopped(self) -> bool:
        """Return whether any stop condition has been recorded."""
        return self.timed_out or self.cancelled_by_user or self.run_cancelled


def _find_repository_top(search_root: Path) -> Path:
    """Return the closest ancestor holding a ``.git`` entry, else the root itself."""
    for directory in (search_root, *search_root.parents):
        if (directory / ".git").exists():
            return directory
    return search_root


class GitIgnoreFilter:
    """Evaluates ``.gitignore`` rules for paths under a search root, git-style.

    Patterns are read lazily per directory from the repository top down to the
    path's parent; across levels the deepest matching pattern wins, matching
    git's precedence. Re-inclusion below an excluded directory is impossible
    because the walker prunes excluded directories before descending — also
    git's behavior. Only ``.gitignore`` files are honored (not
    ``.git/info/exclude`` or the user's global excludes file).
    """

    def __init__(self, search_root: Path) -> None:
        self._top = _find_repository_top(search_root)
        self._patterns_by_directory: dict[Path, list[Pattern] | None] = {}

    def _patterns_for(self, directory: Path) -> list[Pattern] | None:
        if directory in self._patterns_by_directory:
            return self._patterns_by_directory[directory]

        patterns: list[Pattern] | None = None
        gitignore_path = directory / ".gitignore"
        try:
            if gitignore_path.is_file():
                lines = gitignore_path.read_text(encoding="utf-8", errors="replace").splitlines()
                parsed = PathSpec.from_lines("gitignore", lines).patterns
                patterns = [pattern for pattern in parsed if pattern.include is not None] or None
        except OSError:
            patterns = None

        self._patterns_by_directory[directory] = patterns
        return patterns

    def is_ignored(self, path: Path, *, is_directory: bool) -> bool:
        """Return whether git would ignore ``path`` (evaluated as file or directory)."""
        try:
            relative_parts = path.relative_to(self._top).parts
        except ValueError:
            return False

        ignored = False
        for depth in range(len(relative_parts)):
            level_directory = self._top.joinpath(*relative_parts[:depth])
            patterns = self._patterns_for(level_directory)
            if not patterns:
                continue
            candidate = "/".join(relative_parts[depth:])
            if is_directory:
                candidate = f"{candidate}/"
            for pattern in patterns:
                if pattern.match_file(candidate):
                    ignored = bool(pattern.include)
        return ignored


def ignore_rules_apply(search_root: Path, *, include_ignored: bool) -> bool:
    """Decide whether ignore rules filter a search rooted at ``search_root``.

    Rules are off when the caller opted out — or when the root itself is
    ignored: explicitly targeting an ignored directory is intent to search it,
    and filtering would otherwise return a misleading empty result.
    """
    if include_ignored:
        return False
    return not GitIgnoreFilter(search_root).is_ignored(search_root, is_directory=True)


def iter_search_entries(
    search_root: Path,
    *,
    budget: SearchBudget,
    apply_ignore_rules: bool,
    include_directories: bool,
) -> Iterator[tuple[Path, bool]]:
    """Yield ``(path, is_directory)`` under a directory root, deterministically sorted.

    Prunes ignored directories before descending and skips ignored files when
    ``apply_ignore_rules`` is set. ``.git`` internals are always pruned — never
    useful for content search — unless the root itself lies inside a ``.git``
    tree (an explicit reach-in). Polls the budget per directory and per file.
    """
    ignore_filter = GitIgnoreFilter(search_root) if apply_ignore_rules else None
    skip_git_directories = ".git" not in search_root.parts

    for directory_path, directory_names, file_names in os.walk(search_root):
        if not budget.keep_going():
            return
        current = Path(directory_path)

        kept_directories = []
        for name in sorted(directory_names):
            if skip_git_directories and name == ".git":
                continue
            child = current / name
            if ignore_filter is not None and ignore_filter.is_ignored(child, is_directory=True):
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories

        if include_directories:
            for name in kept_directories:
                yield current / name, True

        for name in sorted(file_names):
            if not budget.keep_going():
                return
            # A worktree's .git is a pointer *file*, not a directory.
            if skip_git_directories and name == ".git":
                continue
            child = current / name
            if ignore_filter is not None and ignore_filter.is_ignored(child, is_directory=False):
                continue
            yield child, False


def display_search_path(path: Path, *, cwd: Path) -> str:
    """Render a result path relative to the working directory, absolute outside it.

    Relative tool paths resolve against the working directory, so a result
    rendered this way always round-trips into a follow-up read/apply_patch call —
    regardless of which search root produced it.
    """
    try:
        return model_path(path.relative_to(cwd))
    except ValueError:
        return model_path(path)


def _character_class_end(pattern: str, start: int) -> int | None:
    """Return the index just past a character class at ``start``, or ``None``.

    A ``[`` opens a class only when a closing ``]`` follows; without one the
    bracket is an ordinary literal character. A ``]`` directly after ``[`` (or
    after ``[!``/``[^``) is a literal class member (fnmatch ``[]]``), so the
    first *closing* bracket is searched from the position after it.
    """
    index = start + 1
    if index < len(pattern) and pattern[index] in "!^":
        index += 1
    if index < len(pattern) and pattern[index] == "]":
        index += 1
    closing = pattern.find("]", index)
    if closing == -1:
        return None
    return closing + 1


def _first_expandable_brace(pattern: str) -> int:
    """Return the first ``{`` that is not inside a character class, or -1."""
    index = 0
    while index < len(pattern):
        if pattern[index] == "[":
            class_end = _character_class_end(pattern, index)
            if class_end is not None:
                index = class_end
                continue
        if pattern[index] == "{":
            return index
        index += 1
    return -1


def _expand_brace_alternations(pattern: str) -> list[str]:
    """Expand ``{a,b}`` alternations in a glob pattern, rg ``--glob`` style.

    ``fnmatch`` has no brace support, so without expansion a pattern like
    ``**/*.{py,js}`` would require a literal ``{py,js}`` in the file name and
    silently match nothing. Only groups containing a top-level comma expand;
    comma-less braces (far more likely a literal file name) stay as-is, as do
    unmatched braces. Braces inside a character class ``[...]`` are literal
    class members (rg semantics) and never open or close a group. Nested
    groups expand recursively.
    """
    start = _first_expandable_brace(pattern)
    if start == -1:
        return [pattern]

    depth = 0
    group_start = start
    index = start
    while index < len(pattern):
        char = pattern[index]
        if char == "[":
            class_end = _character_class_end(pattern, index)
            if class_end is not None:
                index = class_end
                continue
        if char == "{":
            if depth == 0:
                group_start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return [pattern]
            if depth != 0:
                index += 1
                continue
            group = pattern[group_start + 1 : index]
            if "," not in group:
                index += 1
                continue
            alternatives: list[str] = []
            segment = ""
            segment_depth = 0
            group_index = 0
            while group_index < len(group):
                group_char = group[group_index]
                if group_char == "[":
                    class_end = _character_class_end(group, group_index)
                    if class_end is not None:
                        segment += group[group_index:class_end]
                        group_index = class_end
                        continue
                if group_char == "{":
                    segment_depth += 1
                elif group_char == "}":
                    segment_depth -= 1
                if group_char == "," and segment_depth == 0:
                    alternatives.append(segment)
                    segment = ""
                else:
                    segment += group_char
                group_index += 1
            alternatives.append(segment)

            expanded: list[str] = []
            prefix = pattern[:group_start]
            suffix = pattern[index + 1 :]
            for alternative in alternatives:
                for candidate in _expand_brace_alternations(prefix + alternative + suffix):
                    if candidate not in expanded:
                        expanded.append(candidate)
                        if len(expanded) > 1024:
                            raise ValueError(
                                "Glob expands to more than 1024 alternatives; split patterns."
                            )
            return expanded
        index += 1
    return [pattern]
