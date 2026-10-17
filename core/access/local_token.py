"""The local access token: the host's own credential for its local tools.

Every server start creates a fresh random token. Once the server listens, it
writes the token to ``<data-dir>/runtime/local-access-<port>.token``, an
owner-only JSON file, together with the loopback addresses it listens on
(``{"token": "vbl_...", "loopback": ["127.0.0.1"]}``; a wildcard bind counts as
the loopback address of its family). The local CLI, tray, Desktop and Agent
shells running ``vbot`` read it and send the token as
``Authorization: Bearer <token>``, but only in a plain ``http://`` request to one
of those addresses as an IP literal on that port (:meth:`LocalAccess.admits`):
a host name such as ``localhost`` may resolve to an address that another
program holds (``::1`` while the server holds ``127.0.0.1``), and any other
address is not where this server listens. The server accepts the token only
from a direct loopback peer without forwarded headers, so it is useless through
a reverse proxy even if it leaks. A client that gets a 401 re-reads the file:
the token changes with every start. The server removes the file when it stops,
unless a newer server has replaced it.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from core.utils.atomic import atomic_write_text
from core.utils.server_control import CONTROL_DIRECTORY_NAME

LOCAL_TOKEN_PREFIX = "vbl_"
_TOKEN_PATTERN = re.compile(r"vbl_[A-Za-z0-9_-]{43}")
_MAX_FILE_BYTES = 1024
#: A wildcard bind serves the loopback address of its family.
_WILDCARD_LOOPBACK = {"0.0.0.0": "127.0.0.1", "::": "::1"}


@dataclass(frozen=True)
class LocalAccessToken:
    """The token one server process created, and the file it is published in."""

    path: Path
    value: str = field(repr=False)


@dataclass(frozen=True)
class LocalAccess:
    """A running server's local access token and where a client may send it."""

    token: str = field(repr=False)
    port: int
    #: Loopback IP literals the server listens on, in compressed form.
    loopback: tuple[str, ...]

    def admits(self, url: str) -> bool:
        """Whether a request to ``url`` reaches this server directly and may carry the token.

        Only plain ``http://`` to one of :attr:`loopback` as an IP literal on
        :attr:`port`; never a host name, even one that resolves to loopback.
        """
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return False
        if parts.scheme != "http" or (port or 80) != self.port:
            return False
        address = loopback_literal(parts.hostname or "")
        return address is not None and address in self.loopback


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
    """Create this server start's token; it is published once the server listens.

    A file an earlier server left behind is removed: this server holds the
    port's control claim, so that file can only name a server that is gone.
    """
    path = local_token_path(data_dir, port)
    path.unlink(missing_ok=True)
    return LocalAccessToken(path, LOCAL_TOKEN_PREFIX + secrets.token_urlsafe(32))


def publish_local_access_token(token: LocalAccessToken, listening: Iterable[str]) -> None:
    """Write the token file for the addresses the server's sockets are bound to.

    ``listening`` holds the bound socket addresses; :func:`loopback_addresses`
    keeps the loopback ones a local client connects to.
    """
    document = {"token": token.value, "loopback": list(loopback_addresses(listening))}
    atomic_write_text(token.path, json.dumps(document) + "\n", owner_only=True)


def loopback_addresses(listening: Iterable[str]) -> tuple[str, ...]:
    """The loopback IP literals a local client reaches a server bound to ``listening`` at."""
    found: list[str] = []
    for bound in listening:
        text = str(bound).strip().removeprefix("[").removesuffix("]")
        address = _WILDCARD_LOOPBACK.get(text) or loopback_literal(text)
        if address is not None and address not in found:
            found.append(address)
    return tuple(found)


def loopback_literal(host: str) -> str | None:
    """The compressed form of a loopback IP literal; ``None`` for names and other addresses.

    IPv4-mapped IPv6 and zoned literals are not accepted: they would name the
    same socket under another spelling.
    """
    try:
        address = ipaddress.ip_address(host.strip().removeprefix("[").removesuffix("]"))
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and (
        address.scope_id is not None or address.ipv4_mapped is not None
    ):
        return None
    return address.compressed if address.is_loopback else None


def read_local_access(data_dir: str | Path, port: int) -> LocalAccess | None:
    """The token and loopback addresses of that server, or ``None`` without a valid file."""
    try:
        with local_token_path(data_dir, port).open("rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES + 1)
    except OSError, ValueError:
        return None
    if len(raw) > _MAX_FILE_BYTES:
        return None
    try:
        document = json.loads(raw.decode("ascii"))
    except UnicodeError, ValueError:
        return None
    if not isinstance(document, dict):
        return None
    token, loopback = document.get("token"), document.get("loopback")
    if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        return None
    if not isinstance(loopback, list) or not all(isinstance(item, str) for item in loopback):
        return None
    addresses = tuple(
        literal for literal in (loopback_literal(item) for item in loopback) if literal is not None
    )
    return LocalAccess(token=token, port=port, loopback=addresses)


def read_local_access_token(data_dir: str | Path, port: int) -> str | None:
    """The current token of that server, or ``None`` when there is no valid file."""
    access = read_local_access(data_dir, port)
    return None if access is None else access.token


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
