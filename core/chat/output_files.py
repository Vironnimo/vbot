"""Recognition of server-local files intentionally named in Assistant replies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.chat.errors import ChatMessageValidationError

JsonObject = dict[str, Any]

_FENCE_PATTERN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_MARKDOWN_WRAPPERS = ("***", "___", "**", "__", "*", "_", "`")
_MARKDOWN_WRAPPER_PATTERN = "|".join(re.escape(wrapper) for wrapper in _MARKDOWN_WRAPPERS)
_FILE_MARKER_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<wrapper>{_MARKDOWN_WRAPPER_PATTERN})?"
    r"(?P<marker>file:(?P<path>\S+))"
)
_TRAILING_PROSE_DELIMITERS = frozenset('.,;!?)]}"`')


@dataclass(frozen=True)
class AssistantFileReference:
    """One resolved regular file marked inside an Assistant-content line."""

    line_index: int
    path: str
    start_index: int | None = None
    end_index: int | None = None

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {"line_index": self.line_index, "path": self.path}
        if self.start_index is not None:
            payload["start_index"] = self.start_index
            payload["end_index"] = self.end_index
        return payload

    @classmethod
    def from_dict(cls, data: Any) -> AssistantFileReference:
        if not isinstance(data, dict):
            raise ChatMessageValidationError("output_files entries must be objects")
        line_index = data.get("line_index")
        if isinstance(line_index, bool) or not isinstance(line_index, int) or line_index < 0:
            raise ChatMessageValidationError(
                "output_files line_index must be a non-negative integer"
            )
        path = data.get("path")
        if not isinstance(path, str) or not path:
            raise ChatMessageValidationError("output_files path must be a non-empty string")
        start_index = data.get("start_index")
        end_index = data.get("end_index")
        if (start_index is None) != (end_index is None):
            raise ChatMessageValidationError(
                "output_files start_index and end_index must be provided together"
            )
        if start_index is not None and (
            isinstance(start_index, bool)
            or not isinstance(start_index, int)
            or start_index < 0
            or isinstance(end_index, bool)
            or not isinstance(end_index, int)
            or end_index <= start_index
        ):
            raise ChatMessageValidationError(
                "output_files spans must be increasing non-negative integers"
            )
        return cls(
            line_index=line_index,
            path=path,
            start_index=start_index,
            end_index=end_index,
        )


def resolve_assistant_file_references(
    content: str | None,
    *,
    cwd: Path | None,
) -> list[AssistantFileReference] | None:
    """Resolve explicit ``file:<path>`` tokens without reading or copying file bytes."""
    if not content:
        return None

    references: list[AssistantFileReference] = []
    open_fence: tuple[str, int] | None = None
    for line_index, line in enumerate(content.splitlines()):
        fence_match = _FENCE_PATTERN.match(line)
        if fence_match is not None:
            marker = fence_match.group(1)
            if open_fence is None:
                open_fence = (marker[0], len(marker))
            elif (
                marker[0] == open_fence[0]
                and len(marker) >= open_fence[1]
                and not fence_match.group(2).strip()
            ):
                open_fence = None
            continue
        if open_fence is not None:
            continue
        if line.startswith(("\t", "    ")):
            continue
        for match in _FILE_MARKER_PATTERN.finditer(line):
            wrapper = match.group("wrapper")
            resolved_marker = _resolve_marked_path(
                match.group("path"),
                wrapper=wrapper,
                cwd=cwd,
            )
            if resolved_marker is None:
                continue
            resolved, path_length, closing_wrapper_length = resolved_marker
            start_index = match.start() if closing_wrapper_length else match.start("marker")
            end_index = match.start("path") + path_length + closing_wrapper_length
            references.append(
                AssistantFileReference(
                    line_index=line_index,
                    path=str(resolved),
                    start_index=start_index,
                    end_index=end_index,
                )
            )
    return references or None


def _resolve_marked_path(
    candidate: str,
    *,
    wrapper: str | None,
    cwd: Path | None,
) -> tuple[Path, int, int] | None:
    """Resolve one whitespace-bounded token while preserving adjacent prose punctuation."""
    if wrapper is not None:
        wrapped = _resolve_wrapped_marked_path(candidate, wrapper=wrapper, cwd=cwd)
        if wrapped is not None:
            resolved, path_length = wrapped
            return resolved, path_length, len(wrapper)

    end_index = len(candidate)
    while end_index:
        unwrapped_path = _resolve_regular_file(candidate[:end_index], cwd=cwd)
        if unwrapped_path is not None:
            return unwrapped_path, end_index, 0
        if candidate[end_index - 1] not in _TRAILING_PROSE_DELIMITERS:
            return None
        end_index -= 1
    return None


def _resolve_wrapped_marked_path(
    candidate: str,
    *,
    wrapper: str,
    cwd: Path | None,
) -> tuple[Path, int] | None:
    """Resolve a marker whose matching Markdown wrapper may precede prose punctuation."""
    end_index = len(candidate)
    while end_index:
        wrapped_candidate = candidate[:end_index]
        if wrapped_candidate.endswith(wrapper):
            path_length = end_index - len(wrapper)
            resolved = _resolve_regular_file(candidate[:path_length], cwd=cwd)
            if resolved is not None:
                return resolved, path_length
        if candidate[end_index - 1] not in _TRAILING_PROSE_DELIMITERS:
            return None
        end_index -= 1
    return None


def _resolve_regular_file(candidate: str, *, cwd: Path | None) -> Path | None:
    try:
        path = Path(candidate).expanduser()
        if not path.is_absolute() and cwd is None:
            return None
        if not path.is_absolute():
            assert cwd is not None
            path = cwd / path
        resolved = path.resolve()
        return resolved if resolved.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None
