"""Shared internal helpers for file-search tools."""

from __future__ import annotations

import fnmatch
import time
from functools import cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    """

    def __init__(self, context: ToolContext, timeout_seconds: float | None = None) -> None:
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

    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (context.effective_cwd / candidate).resolve()


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


def file_filter_matches(relative_path: str, pattern: str) -> bool:
    """Return whether a forward-slash relative path matches a file filter pattern."""
    normalized_path = normalize_file_filter_pattern(relative_path, field_name="path")
    normalized_pattern = normalize_file_filter_pattern(pattern)

    if "/" not in normalized_pattern:
        if normalized_pattern == "**":
            return True
        return fnmatch.fnmatch(PurePosixPath(normalized_path).name, normalized_pattern)

    path_segments = tuple(segment for segment in normalized_path.split("/") if segment)
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
        if not fnmatch.fnmatch(path_segments[path_index], pattern_segment):
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
    "SearchBudget",
    "cap_output_bytes",
    "display_search_path",
    "file_filter_matches",
    "normalize_file_filter_pattern",
    "relative_forward_path",
    "render_limited_results",
    "resolve_search_path",
]
