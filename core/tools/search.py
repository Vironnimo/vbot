"""Shared internal helpers for file-search tools."""

from __future__ import annotations

import fnmatch
import os
import time
from functools import cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from pathspec import PathSpec
from pathspec.pattern import Pattern

if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.tools.tools import ToolContext

SEARCH_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 50 * 1024
OUTPUT_TRUNCATED_MARKER = "[... output truncated ...]"
RESULTS_LIMITED_MARKER = "[Results limited to {limit} matches.]"
SEARCH_TIMEOUT_MARKER = "[Search timed out; results may be incomplete.]"
SEARCH_CANCELLED_FAILURE_CODE = "cancelled_by_user"
SEARCH_CANCELLED_FAILURE_MESSAGE = "Search aborted by the user"


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
    rendered this way always round-trips into a follow-up read/edit call —
    regardless of which search root produced it.
    """
    try:
        return path.relative_to(cwd).as_posix()
    except ValueError:
        return path.as_posix()


def cap_output_bytes(content: str, *, trailing_lines: list[str] | None = None) -> str:
    """Cap rendered output at ``MAX_OUTPUT_BYTES``, keeping trailing marker lines."""
    encoded = content.encode("utf-8")
    suffix = "".join(f"\n{line}" for line in (trailing_lines or []) if line)
    suffix_bytes = suffix.encode("utf-8")
    if len(encoded) + len(suffix_bytes) <= MAX_OUTPUT_BYTES:
        return content + suffix

    marker = f"\n{OUTPUT_TRUNCATED_MARKER}{suffix}"
    marker_bytes = marker.encode("utf-8")
    keep_bytes = max(MAX_OUTPUT_BYTES - len(marker_bytes), 0)
    clipped = encoded[:keep_bytes].decode("utf-8", errors="ignore")
    return clipped + marker


def render_limited_results(
    lines: list[str],
    *,
    observed_results: int,
    limit: int,
    timed_out: bool = False,
) -> str:
    """Join result lines, appending explicit limit/timeout markers within the byte cap."""
    if not lines:
        return ""
    trailing_lines: list[str] = []
    if observed_results > limit:
        trailing_lines.append(RESULTS_LIMITED_MARKER.format(limit=limit))
    if timed_out:
        trailing_lines.append(SEARCH_TIMEOUT_MARKER)
    return cap_output_bytes("\n".join(lines), trailing_lines=trailing_lines)


def resolve_search_path(context: ToolContext, path: str | None) -> Path:
    """Resolve an optional tool path against the tool working directory.

    Search tools default to the working directory (``ToolContext.effective_cwd``:
    the project repo in a project session, otherwise the agent workspace).
    Supplied paths may be absolute or relative to that directory, and ``~`` is
    expanded before resolution.
    """
    if path is None:
        return context.effective_cwd.expanduser().resolve()
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")

    return context.resolve_path(path)


def normalize_file_filter_pattern(
    pattern: str,
    *,
    field_name: str = "glob",
    allow_empty: bool = False,
) -> str:
    """Normalize a glob-style file filter to a forward-slash relative pattern."""
    if not isinstance(pattern, str):
        raise ValueError(f"{field_name} must be a string")

    normalized = pattern.strip().replace("\\", "/")
    if not normalized:
        if allow_empty:
            return ""
        raise ValueError(f"{field_name} must not be empty")

    if _is_absolute_or_user_rooted(normalized):
        raise ValueError(f"{field_name} must be a relative file pattern")

    parts: list[str] = []
    for segment in normalized.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            raise ValueError(f"{field_name} must not contain '..' segments")
        parts.append(segment)

    if not parts:
        if allow_empty:
            return ""
        raise ValueError(f"{field_name} must not be empty")

    return "/".join(parts)


def relative_forward_path(path: Path, *, base: Path) -> str:
    """Return ``path`` relative to ``base`` using forward slashes."""
    return path.relative_to(base).as_posix()


def _relative_path_segments(relative_path: str) -> tuple[str, ...]:
    """Split a relative path into segments without pattern validation.

    Result paths are data, not patterns: a file legitimately named ``~lock``
    or ``..data`` must not trip the pattern-side validation rules.
    """
    return tuple(
        segment
        for segment in relative_path.replace("\\", "/").split("/")
        if segment not in {"", "."}
    )


def file_filter_matches(relative_path: str, pattern: str) -> bool:
    """Return whether a relative path matches a file filter pattern.

    A pattern without ``/`` matches the file name at any depth (rg ``--glob``
    semantics); matching is case-insensitive on every platform.
    """
    path_segments = _relative_path_segments(relative_path)
    normalized_pattern = normalize_file_filter_pattern(pattern)

    if "/" not in normalized_pattern:
        if normalized_pattern == "**":
            return True
        name = path_segments[-1] if path_segments else ""
        return fnmatch.fnmatchcase(name.casefold(), normalized_pattern.casefold())

    pattern_segments = tuple(segment for segment in normalized_pattern.split("/") if segment)
    return _match_glob_path_segments(path_segments, pattern_segments)


def glob_path_matches(relative_path: str, pattern: str) -> bool:
    """Return whether a relative path matches an anchored glob pattern.

    Unlike ``file_filter_matches`` there is no bare-name shortcut: ``*.py``
    matches top-level entries only, ``**/*.py`` matches at any depth —
    standard glob semantics. Case-insensitive on every platform.
    """
    path_segments = _relative_path_segments(relative_path)
    normalized_pattern = normalize_file_filter_pattern(pattern)
    pattern_segments = tuple(segment for segment in normalized_pattern.split("/") if segment)
    return _match_glob_path_segments(path_segments, pattern_segments)


def _is_absolute_or_user_rooted(pattern: str) -> bool:
    if pattern.startswith("~"):
        return True
    if PurePosixPath(pattern).is_absolute():
        return True

    windows_path = PureWindowsPath(pattern)
    return windows_path.is_absolute() or bool(windows_path.drive)


def _match_glob_path_segments(
    path_segments: tuple[str, ...], pattern_segments: tuple[str, ...]
) -> bool:
    @cache
    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_segments):
            return path_index == len(path_segments)

        pattern_segment = pattern_segments[pattern_index]
        if pattern_segment == "**":
            next_pattern_index = pattern_index
            while (
                next_pattern_index < len(pattern_segments)
                and pattern_segments[next_pattern_index] == "**"
            ):
                next_pattern_index += 1
            if next_pattern_index == len(pattern_segments):
                return True
            return any(
                matches(next_path_index, next_pattern_index)
                for next_path_index in range(path_index, len(path_segments) + 1)
            )

        if path_index >= len(path_segments):
            return False
        if not fnmatch.fnmatchcase(
            path_segments[path_index].casefold(), pattern_segment.casefold()
        ):
            return False
        return matches(path_index + 1, pattern_index + 1)

    return matches(0, 0)


__all__ = [
    "MAX_OUTPUT_BYTES",
    "OUTPUT_TRUNCATED_MARKER",
    "RESULTS_LIMITED_MARKER",
    "SEARCH_CANCELLED_FAILURE_CODE",
    "SEARCH_CANCELLED_FAILURE_MESSAGE",
    "SEARCH_TIMEOUT_MARKER",
    "SEARCH_TIMEOUT_SECONDS",
    "GitIgnoreFilter",
    "SearchBudget",
    "cap_output_bytes",
    "display_search_path",
    "file_filter_matches",
    "glob_path_matches",
    "ignore_rules_apply",
    "iter_search_entries",
    "normalize_file_filter_pattern",
    "relative_forward_path",
    "render_limited_results",
    "resolve_search_path",
]
