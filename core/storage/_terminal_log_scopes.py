"""Launch-scope provenance for raw Terminal logs in temporary storage."""

from __future__ import annotations

import hashlib
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from core.utils.file_status import is_link_status

TERMINAL_TEMPORARY_CATEGORY = "terminals"
SCOPE_NAME = re.compile(r"p[0-9a-f]{64}\Z")
SCOPE_SUFFIX = ".scope"
_MAX_SCOPE_BYTES = 128 * 1024
_SCOPE_CONTENT = re.compile(rb"(?:[0-9a-f]{64}\n)+\Z")


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).hexdigest().encode("ascii")


def scope_record(scope: frozenset[str]) -> tuple[str, bytes]:
    """Content-address an immutable record of the launch's layer fingerprints."""
    data = b"".join(sorted(_digest(layer.encode("utf-8")) + b"\n" for layer in scope))
    if not data or len(data) > _MAX_SCOPE_BYTES:
        raise ValueError("Invalid Terminal log launch scope")
    return "p" + _digest(data).decode("ascii"), data


def read_scope_record(path: Path) -> bytes | None:
    """Read a bounded regular provenance record; missing or unreadable means none."""
    try:
        status = path.stat(follow_symlinks=False)
        if is_link_status(status) or not stat.S_ISREG(status.st_mode):
            return None
        with path.open("rb") as stream:
            data = stream.read(_MAX_SCOPE_BYTES + 1)
    except OSError:
        return None
    return data if len(data) <= _MAX_SCOPE_BYTES else None


@dataclass(frozen=True, slots=True)
class TerminalLogScope:
    """Read only scoped groups of one Session below the Terminal log category."""

    root: Path
    owner: str | None

    def allows(self, relative: str, current_scope: frozenset[str]) -> bool:
        owner, slash, rest = relative.partition("/")
        if not slash or owner != self.owner or not current_scope:
            return False
        name = rest.partition("/")[0]
        if SCOPE_NAME.fullmatch(name) is None:
            return False
        marker = self.root / owner / (name + SCOPE_SUFFIX)
        data = read_scope_record(marker)
        if (
            not data
            or name.encode("ascii") != b"p" + _digest(data)
            or _SCOPE_CONTENT.fullmatch(data) is None
        ):
            return False
        recorded = frozenset(data.splitlines())
        return all(_digest(layer.encode("utf-8")) in recorded for layer in current_scope)
