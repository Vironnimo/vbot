"""The local access token: the host's own credential for its local tools.

Every server start writes a fresh random token to
``<data-dir>/runtime/local-access-<port>.token``, an owner-only file. The local
CLI, tray, Desktop and Agent shells running ``vbot`` read it and send it as
``Authorization: Bearer <token>``. The server accepts it only from a direct
loopback peer without forwarded headers, so it is useless through a reverse
proxy even if it leaks. A client that gets a 401 re-reads the file: the token
changes with every start. The server removes the file when it stops, unless a
newer server has replaced it.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from core.utils.atomic import atomic_write_text
from core.utils.server_control import CONTROL_DIRECTORY_NAME

LOCAL_TOKEN_PREFIX = "vbl_"
_TOKEN_PATTERN = re.compile(r"vbl_[A-Za-z0-9_-]{43}")
_MAX_FILE_BYTES = 256


@dataclass(frozen=True)
class LocalAccessToken:
    """The token one server process wrote, and where."""

    path: Path
    value: str


def local_token_path(data_dir: str | Path, port: int) -> Path:
    """The token file of the server serving ``data_dir`` on ``port``."""
    if not 1 <= port <= 65_535:
        raise ValueError("Server port must be between 1 and 65535")
    return (
        Path(data_dir).expanduser().resolve()
        / CONTROL_DIRECTORY_NAME
        / f"local-access-{port}.token"
    )


def create_local_access_token(data_dir: str | Path, port: int) -> LocalAccessToken:
    """Write a fresh token for this server start, replacing any earlier one."""
    value = LOCAL_TOKEN_PREFIX + secrets.token_urlsafe(32)
    path = local_token_path(data_dir, port)
    atomic_write_text(path, value + "\n", owner_only=True)
    return LocalAccessToken(path, value)


def read_local_access_token(data_dir: str | Path, port: int) -> str | None:
    """The current token of that server, or ``None`` when there is no valid one."""
    try:
        with local_token_path(data_dir, port).open("rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES + 1)
    except OSError, ValueError:
        return None
    if len(raw) > _MAX_FILE_BYTES:
        return None
    try:
        value = raw.decode("ascii").strip()
    except UnicodeError:
        return None
    return value if _TOKEN_PATTERN.fullmatch(value) else None


def remove_local_access_token(token: LocalAccessToken) -> None:
    """Remove the file when it still holds this token; a newer server keeps its own."""
    current = read_local_access_token(token.path.parent.parent, _port_of(token.path))
    if current is not None and secrets.compare_digest(current, token.value):
        token.path.unlink(missing_ok=True)


def is_local_access_token(value: str) -> bool:
    """Whether ``value`` has the form of a local access token."""
    return _TOKEN_PATTERN.fullmatch(value) is not None


def _port_of(path: Path) -> int:
    return int(path.name.removeprefix("local-access-").removesuffix(".token"))
