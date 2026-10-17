"""Server addresses the Desktop connects to: a host and port, or an ``https://`` URL.

A plain host and port name a server over HTTP on the local computer or the home
network (``http://<host>:<port>/``). A server reached over the internet, usually
behind a reverse proxy, is entered as an ``https://`` URL with an optional port
(``https://vbot.example.com``); an ``http://`` URL is accepted as another
spelling of host and port. Every surface that takes ``(host, port)`` (the
connection screen, the bridge, ``--host``/``--port``, a restart request) passes
such a URL as the host; its own port wins and the port argument is ignored.

:attr:`ServerAddress.bridge_host` gives the host form those surfaces round-trip:
the plain host for HTTP, the URL for HTTPS.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

Scheme = Literal["http", "https"]

HTTPS_DEFAULT_PORT = 443
HTTP_DEFAULT_PORT = 80
# Block URL-structure characters plus HTML/JS metacharacters: none appear in a
# valid host name or IPv4 literal, and barring them keeps a stored host from
# ever carrying a markup/script payload into the connection screen.
INVALID_HOST_CHARACTERS = frozenset("/\\:?#@[]'\"`<>&();")
_URL_MARKER = "://"


@dataclass(frozen=True)
class ServerAddress:
    """One validated server: scheme, host and effective port.

    HTTP hosts keep the spelling they were entered with (stored servers and
    Voice profiles are keyed by it); HTTPS hosts are lower-cased.
    """

    scheme: Scheme
    host: str
    port: int

    @property
    def url(self) -> str:
        """The base URL with a trailing slash.

        HTTP always names the port, as earlier Desktop versions did; HTTPS
        omits its default port.
        """
        if self.scheme == "http":
            return f"http://{self.host}:{self.port}/"
        if self.port == HTTPS_DEFAULT_PORT:
            return f"https://{self.host}/"
        return f"https://{self.host}:{self.port}/"

    @property
    def origin(self) -> str:
        """The serialized web origin: lower-case host, default port omitted."""
        default = HTTP_DEFAULT_PORT if self.scheme == "http" else HTTPS_DEFAULT_PORT
        host = self.host.lower()
        if self.port == default:
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def bridge_host(self) -> str:
        """The host as ``(host, port)`` surfaces carry it: plain for HTTP, the URL for HTTPS."""
        return self.host if self.scheme == "http" else self.url.rstrip("/")

    @property
    def display(self) -> str:
        """How the user sees the server: ``host:port`` or the HTTPS URL."""
        return f"{self.host}:{self.port}" if self.scheme == "http" else self.bridge_host

    @property
    def is_loopback(self) -> bool:
        """Whether the host names this computer's loopback interface."""
        return is_loopback_host(self.host)


def parse_server_address(host: Any, port: Any) -> ServerAddress:
    """Validate ``(host, port)`` into a :class:`ServerAddress`; raises ``ValueError``.

    A ``host`` that contains ``://`` is read as an ``http://`` or ``https://``
    URL without credentials, path, query or fragment; its port (or the scheme's
    default) is used and ``port`` is ignored. Any other host must be a plain
    host name or IPv4 address and ``port`` a valid TCP port.
    """
    if isinstance(host, str) and _URL_MARKER in host:
        return _parse_url(host.strip())
    return ServerAddress("http", validate_host(host), validate_port(port))


def is_loopback_host(host: str) -> bool:
    """Whether a validated host names this computer's loopback interface."""
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_port(value: Any, *, source: str = "port") -> int:
    """Validate a TCP port value."""
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer port") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{source} must be between 1 and 65535")
    return port


def validate_host(value: Any, *, source: str = "host") -> str:
    """Validate a plain host name or IPv4 address (no scheme, port or path)."""
    if not isinstance(value, str):
        raise ValueError(f"{source} must be a host name or IP address")
    host = value.strip()
    if not host:
        raise ValueError(f"{source} must not be empty")
    if any(character.isspace() for character in host):
        raise ValueError(f"{source} must not contain whitespace")
    if any(character in INVALID_HOST_CHARACTERS for character in host):
        raise ValueError(f"{source} must be a host name or IP address, not a URL")
    return host


def _parse_url(value: str) -> ServerAddress:
    if any(character.isspace() for character in value):
        raise ValueError("The server address must not contain whitespace")
    try:
        parts = urlsplit(value)
        explicit_port = parts.port
    except ValueError as exc:
        raise ValueError("The server address is not a valid URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("The server address must start with https:// or http://")
    if parts.username is not None or parts.password is not None:
        raise ValueError("The server address must not contain a user name or password")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("The server address must not contain a path, query or fragment")
    host = validate_host(parts.hostname or "", source="The server address host")
    if scheme == "https":
        port = HTTPS_DEFAULT_PORT if explicit_port is None else explicit_port
        return ServerAddress("https", host.lower(), validate_port(port))
    port = HTTP_DEFAULT_PORT if explicit_port is None else explicit_port
    return ServerAddress("http", host, validate_port(port))
