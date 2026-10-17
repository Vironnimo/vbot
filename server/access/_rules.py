"""Peer, transport and route rules of the access guard: pure functions of one request."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from core.access.config import (
    AccessConfig,
    IpAddress,
    IpNetwork,
    address_in,
    is_loopback_address,
    parse_ip,
)
from core.extensions.oauth_redirects import CALLBACK_PATH as OAUTH_CALLBACK_PATH
from core.utils.server_control import CONTROL_SHUTDOWN_PATH
from server.file_delivery import (
    EXTENSION_ASSET_URL_PREFIX,
    EXTENSION_RUN_URL_PREFIX,
    PREVIEW_URL_PREFIX,
)

#: Headers a reverse proxy adds. In ``local`` and ``lan`` any of them refuses the
#: request: a proxy in front means it may come from anywhere.
_PROXY_HEADERS = frozenset({b"forwarded", b"x-real-ip"})
_PROXY_HEADER_PREFIX = b"x-forwarded-"

AUTH_STATUS_PATH = "/api/auth/status"
PAIR_START_PATH = "/api/auth/pair/start"
PAIR_POLL_PATH = "/api/auth/pair/poll"
TICKET_PATH_PREFIX = "/auth/ticket/"
FILE_URL_PREFIX = "/api/files/"

RouteClass = Literal["exempt", "shell", "protected"]

_EXEMPT_ROUTES = frozenset(
    {
        ("GET", "/health"),
        ("HEAD", "/health"),
        ("GET", AUTH_STATUS_PATH),
        ("POST", PAIR_START_PATH),
        ("POST", PAIR_POLL_PATH),
        ("GET", OAUTH_CALLBACK_PATH),
        ("POST", CONTROL_SHUTDOWN_PATH),
    }
)
#: Routes that verify their own HMAC capability in the URL.
_CAPABILITY_PREFIXES = (
    FILE_URL_PREFIX,
    PREVIEW_URL_PREFIX,
    EXTENSION_ASSET_URL_PREFIX,
    EXTENSION_RUN_URL_PREFIX,
)
#: Server namespaces; everything else a GET reaches is the static WebUI shell.
_SERVER_NAMESPACES = ("/api", "/ws", "/_vbot", "/auth", "/health")

Refusal = Literal[
    "unknown_peer", "proxy_headers", "peer_not_admitted", "invalid_forwarded_for", "insecure"
]


@dataclass(frozen=True)
class RequestFacts:
    """What the guard knows about where one request comes from.

    ``client`` is the effective client: the direct peer, or the address a trusted
    proxy forwarded. ``refusal`` names the broken peer or transport rule.
    """

    peer: IpAddress | None
    client: IpAddress | None
    proxy_headers: bool
    proxied: bool
    secure: bool
    refusal: Refusal | None

    @property
    def direct_loopback(self) -> bool:
        """A loopback peer without any forwarding header: where the local token counts."""
        return self.peer is not None and is_loopback_address(self.peer) and not self.proxy_headers


def inspect_request(
    scope: Mapping[str, Any],
    config: AccessConfig,
    *,
    admitted: tuple[IpNetwork, ...] | None,
    proxies: tuple[IpNetwork, ...],
) -> RequestFacts:
    """Resolve the effective client and apply the mode's peer and transport rules."""
    client_info = scope.get("client")
    peer = parse_ip(str(client_info[0])) if client_info else None
    headers: list[tuple[bytes, bytes]] = list(scope.get("headers") or ())
    proxy_headers = any(
        name in _PROXY_HEADERS or name.startswith(_PROXY_HEADER_PREFIX) for name, _value in headers
    )
    secure = str(scope.get("scheme", "")).casefold() in {"https", "wss"}
    if peer is None:
        return RequestFacts(None, None, proxy_headers, False, secure, "unknown_peer")
    if config.mode != "authenticated":
        refusal: Refusal | None = None
        if proxy_headers:
            refusal = "proxy_headers"
        elif admitted is not None and not address_in(peer, admitted):
            refusal = "peer_not_admitted"
        return RequestFacts(peer, peer, proxy_headers, False, secure, refusal)
    if proxy_headers and address_in(peer, proxies):
        client = _forwarded_client(_header_values(headers, b"x-forwarded-for"), proxies, peer)
        if client is None:
            return RequestFacts(peer, None, True, True, False, "invalid_forwarded_for")
        protocols = _list_values(_header_values(headers, b"x-forwarded-proto"))
        forwarded_secure = bool(protocols) and protocols[-1].casefold() == "https"
        return RequestFacts(
            peer, client, True, True, forwarded_secure, None if forwarded_secure else "insecure"
        )
    # Plain HTTP only from this computer; everyone else needs HTTPS.
    refusal = None if secure or is_loopback_address(peer) else "insecure"
    return RequestFacts(peer, peer, proxy_headers, False, secure, refusal)


def classify_route(scope_type: str, method: str, path: str) -> RouteClass:
    """Whether a route needs a principal in ``authenticated`` mode. Fails closed."""
    if scope_type != "http":
        return "protected"
    if (method, path) in _EXEMPT_ROUTES:
        return "exempt"
    if method in {"GET", "HEAD"}:
        ticket = path.removeprefix(TICKET_PATH_PREFIX)
        if path.startswith(TICKET_PATH_PREFIX) and ticket and "/" not in ticket:
            return "exempt"
    if path.startswith(_CAPABILITY_PREFIXES):
        return "exempt"
    if method in {"GET", "HEAD"} and not any(
        path == namespace or path.startswith(f"{namespace}/") for namespace in _SERVER_NAMESPACES
    ):
        return "shell"
    return "protected"


def _header_values(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in headers if key == name]


def _list_values(values: list[str]) -> list[str]:
    """Every comma-separated element of repeated headers, in order."""
    return [item.strip() for value in values for item in value.split(",") if item.strip()]


def _forwarded_client(
    values: list[str], proxies: tuple[IpNetwork, ...], peer: IpAddress
) -> IpAddress | None:
    """The rightmost forwarded address that is not a trusted proxy.

    Addresses left of it were written by the client itself and prove nothing.
    ``None`` refuses a chain with an entry that is not an address.
    """
    chain = _list_values(values)
    if not chain:
        return peer
    parsed: list[IpAddress] = []
    for entry in chain:
        address = _forwarded_address(entry)
        if address is None:
            return None
        parsed.append(address)
    for address in reversed(parsed):
        if not address_in(address, proxies):
            return address
    return parsed[0]


def _forwarded_address(entry: str) -> IpAddress | None:
    address = parse_ip(entry)
    if address is not None:
        return address
    # Some proxies append the client port: "203.0.113.7:51234" or "[2001:db8::7]:51234".
    if entry.startswith("[") and "]:" in entry:
        return parse_ip(entry[1 : entry.index("]")])
    host, separator, port = entry.rpartition(":")
    if separator and port.isdigit() and "." in host:
        return parse_ip(host)
    return None
