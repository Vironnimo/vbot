"""Recognition of server-local files intentionally named in Assistant replies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.chat.errors import ChatMessageValidationError

JsonObject = dict[str, Any]

_FENCE_PATTERN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_URL_SCHEME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass(frozen=True)
class AssistantFileReference:
    """One resolved regular file named on a standalone Assistant-content line."""

    line_index: int
    path: str

    def to_dict(self) -> JsonObject:
        return {"line_index": self.line_index, "path": self.path}

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
        return cls(line_index=line_index, path=path)


def resolve_assistant_file_references(
    content: str | None,
    *,
    cwd: Path | None,
) -> list[AssistantFileReference] | None:
    """Resolve explicit standalone path lines without reading or copying file bytes."""
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

        candidate = _standalone_path_candidate(line)
        if candidate is None:
            continue
        resolved = _resolve_regular_file(candidate, cwd=cwd)
        if resolved is None:
            continue
        references.append(AssistantFileReference(line_index=line_index, path=str(resolved)))
    return references or None


def _standalone_path_candidate(line: str) -> str | None:
    if line.startswith(("\t", "    ")):
        return None
    candidate = line.strip()
    if not candidate or "\x00" in candidate or _URL_SCHEME_PATTERN.match(candidate):
        return None
    if len(candidate) >= 2 and candidate.startswith("`") and candidate.endswith("`"):
        candidate = candidate[1:-1].strip()
    if not candidate or "\x00" in candidate:
        return None
    return candidate


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
