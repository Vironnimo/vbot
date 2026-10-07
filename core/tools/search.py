"""Shared internal helpers for file-search tools.

Besides the budget and path display ``search_files`` uses, this module lists files
for callers outside a Tool call, such as the ``@`` file picker of Chat, with the
same selection ``search_files`` makes when it lists files.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from core.tools._search_execution import (
    ALWAYS_EXCLUDED,
    DEFAULT_ARGUMENTS,
    NativeOutcome,
    native_lines,
)
from core.utils.paths import model_path

if TYPE_CHECKING:
    from collections.abc import Iterable

    from core.tools.tools import ToolContext

SEARCH_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 50 * 1024

# The selection of a search_files listing without arguments.
_SELECTION = (*DEFAULT_ARGUMENTS, f"--glob={ALWAYS_EXCLUDED}")


class SearchBudget:
    """Cooperative stop signal for one search tool call.

    Search handlers run in a worker thread, so nothing external can interrupt
    a long filesystem walk: the loops must poll. ``keep_going()`` folds the
    three stop reasons (user cancel, run cancel, wall-clock timeout) into one
    check and records which one fired, so the handler can decide between a
    cancel failure envelope and partial results with a timeout marker.

    ``context=None`` builds a timeout-only budget for listings that run outside a
    tool call (e.g. the ``files.list`` RPC listing) — no cancel signals exist
    there, so only the wall clock stops the listing.
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


def list_selected_files(
    binary: Path, root: Path, budget: SearchBudget, *, limit: int
) -> tuple[list[str], bool]:
    """Return up to ``limit`` files that ``search_files`` lists under ``root``.

    The selection is the search engine's own: hidden files are included;
    ``.gitignore``, ``.ignore``, ``.rgignore``, ``.git/info/exclude`` and the
    global Git excludes apply; ``.git`` and directory links are never entered.
    Paths are relative to ``root`` with ``/`` separators, in no particular order.
    The second value says whether the list may be incomplete: ``limit`` cut it or
    ``budget`` stopped it. A name with a line break, possible outside Windows,
    comes out split at it.
    """
    files: list[str] = []
    truncated = False
    # Unreadable directories are left out, as search_files leaves them out of a listing.
    outcome = NativeOutcome()
    lines = native_lines(binary, [*_SELECTION, "--files"], None, budget, cwd=root, outcome=outcome)
    with contextlib.closing(lines):
        for line in lines:
            if len(files) >= limit:
                truncated = True
                break
            name = os.fsdecode(line.removesuffix(b"\n"))
            # Only Windows prints another separator; there it cannot occur in a name.
            files.append(name.replace(os.sep, "/") if os.sep != "/" else name)
    return files, truncated or budget.stopped


def unselected_names(
    binary: Path, root: Path, directory: str, names: Iterable[str], budget: SearchBudget
) -> set[str]:
    """Return the ``names`` in ``directory`` that :func:`list_selected_files` leaves out.

    ``directory`` is relative to ``root`` with ``/`` separators (``""`` is ``root``).
    An entry is left out when an ignore rule or the ``.git`` exclusion skips it, and
    every entry is when ``directory`` lies in a skipped directory. A stopped
    ``budget`` leaves the answer incomplete; check it afterwards.
    """
    parts = [part for part in directory.split("/") if part]
    # One level of each directory from the root down: the skips at each level show
    # whether the next directory down, and finally each entry, is left out.
    walked = [os.path.join(*parts[:depth]) if depth else "." for depth in range(len(parts) + 1)]
    outcome = NativeOutcome()
    arguments = [*_SELECTION, "--files", "--debug", "--max-depth=1", "--", *walked]
    lines = native_lines(binary, arguments, None, budget, cwd=root, outcome=outcome)
    with contextlib.closing(lines):
        for _line in lines:
            pass
    skipped = {_comparable(root / os.fsdecode(path)) for path in outcome.skipped}
    levels = (root.joinpath(*parts[:depth]) for depth in range(1, len(parts) + 1))
    if any(_comparable(level) in skipped for level in levels):
        return set(names)
    base = root.joinpath(*parts)
    return {name for name in names if _comparable(base / name) in skipped}


def _comparable(path: Path) -> str:
    return os.path.normcase(os.path.normpath(path))


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
