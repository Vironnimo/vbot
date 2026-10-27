"""An Identity Agent's SOUL.md as one revisioned text document.

The user edits it outside the Agent's own Tools, while the Agent may rewrite it
at any time; a write therefore names the revision it was based on and is
refused when the file changed since.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from core.agents._types import AgentError
from core.utils.atomic import atomic_write_text

SOUL_FILE_NAME = "SOUL.md"


@dataclass(frozen=True)
class SoulFile:
    """The text of a SOUL.md, the revision it has, and where it lives.

    ``revision`` is empty for a missing file, otherwise the SHA-256 of its bytes.
    """

    content: str
    revision: str
    path: str


class SoulFileError(AgentError):
    """SOUL.md cannot be read as UTF-8 text or cannot be written."""


class SoulConflictError(AgentError):
    """A SOUL.md write was based on a revision that is no longer current."""

    def __init__(self, current: SoulFile) -> None:
        super().__init__("SOUL.md changed since it was read; reload it and apply your change again")
        self.current = current


def read_soul(workspace: Path) -> SoulFile:
    path = workspace / SOUL_FILE_NAME
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return SoulFile(content="", revision="", path=str(path))
    except OSError as error:
        raise SoulFileError(f"SOUL.md cannot be read: {error.strerror or error}") from error
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SoulFileError("SOUL.md is not UTF-8 text") from error
    return SoulFile(content=content, revision=hashlib.sha256(data).hexdigest(), path=str(path))


def write_soul(workspace: Path, content: str, *, expected_revision: str) -> SoulFile:
    current = read_soul(workspace)
    if current.revision != expected_revision:
        raise SoulConflictError(current)
    try:
        atomic_write_text(workspace / SOUL_FILE_NAME, content)
    except OSError as error:
        raise SoulFileError(f"SOUL.md cannot be written: {error.strerror or error}") from error
    return read_soul(workspace)
