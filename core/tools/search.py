"""Shared internal helpers for file-search tools."""

from __future__ import annotations

import fnmatch
from functools import cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.tools.tools import ToolContext


def resolve_search_path(context: ToolContext, path: str | None) -> Path:
    """Resolve an optional tool path against the workspace.

    Search tools default to the agent workspace. Supplied paths may be absolute
    or relative to the workspace, and ``~`` is expanded before resolution.
    """
    if path is None:
        return context.workspace.expanduser().resolve()
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")

    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (context.workspace / candidate).resolve()


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
    "file_filter_matches",
    "normalize_file_filter_pattern",
    "relative_forward_path",
    "resolve_search_path",
]
