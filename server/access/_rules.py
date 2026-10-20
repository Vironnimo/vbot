"""Peer, transport and route rules of the access guard: pure functions of one request."""

from __future__ import annotations

import dataclasses
import functools
import re
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from core.access.config import (
    AccessConfig,
    IpAddress,
    IpNetwork,
    address_in,
    is_loopback_address,
    normalize_host_name,
    parse_ip,
)
from core.automation.cron import WEBHOOK_PATH_PREFIX
from core.extensions.oauth_redirects import CALLBACK_PATH as OAUTH_CALLBACK_PATH
from core.utils.server_control import CONTROL_SHUTDOWN_PATH
from server.file_delivery import (
    EXTENSION_ASSET_URL_PREFIX,
    EXTENSION_RUN_URL_PREFIX,
    FILE_URL_PREFIX,
    PREVIEW_URL_PREFIX,
)

#: Headers a reverse proxy or CDN adds. In ``local`` and ``lan`` any of them
#: refuses the request: a proxy in front means it may come from anywhere.
_PROXY_HEADERS = frozenset(
    {
        b"forwarded",
        b"via",
        b"x-real-ip",
        b"x-client-ip",
        b"x-cluster-client-ip",
        b"x-original-forwarded-for",
        b"true-client-ip",
        b"cf-connecting-ip",
        b"fastly-client-ip",
    }
)
_PROXY_HEADER_PREFIX = b"x-forwarded-"

AUTH_STATUS_PATH = "/api/auth/status"
PAIR_START_PATH = "/api/auth/pair/start"
PAIR_POLL_PATH = "/api/auth/pair/poll"
TICKET_PATH_PREFIX = "/auth/ticket/"

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
#: The GET/HEAD routes that verify their own HMAC capability in the URL, by exact
#: shape: ``<prefix><capability>`` or ``<prefix><capability>/<rest>``.
_CAPABILITY_ROUTES = re.compile(
    "|".join(
        (
            rf"{re.escape(FILE_URL_PREFIX)}[^/]+",
            rf"{re.escape(PREVIEW_URL_PREFIX)}[^/]+/.*",
            rf"{re.escape(EXTENSION_ASSET_URL_PREFIX)}[^/]+/.*",
            rf"{re.escape(EXTENSION_RUN_URL_PREFIX)}[^/]+/(?:events|timeline)",
        )
    ),
    re.DOTALL,
)
#: Cron webhooks: ``POST <prefix><job id>``. The route checks the job's own
#: secret, which its ``Authorization`` header may carry.
_WEBHOOK_ROUTE = re.compile(rf"{re.escape(WEBHOOK_PATH_PREFIX)}[^/]+")
#: Server namespaces; everything else a GET reaches is the static WebUI shell.
_SERVER_NAMESPACES = ("/api", "/ws", "/_vbot", "/auth", "/health")

Refusal = Literal[
    "unknown_peer",
    "proxy_headers",
    "peer_not_admitted",
    "invalid_forwarded_for",
    "insecure",
    "host_not_allowed",
]


@dataclass(frozen=True)
class HostPolicy:
    """The names a request's ``Host`` may carry; IP addresses always pass.

    DNS rebinding reaches the server through a name the attacker controls, so
    the server answers only to names it knows as its own: ``localhost``, this
    machine's names, its bind address, the public URL's host and ``--allow-host``.
    An IP address names the server itself, never a foreign site.
    """

    names: frozenset[str]

    def allows(self, headers: Iterable[tuple[bytes, bytes]]) -> bool:
        values = _header_values(headers, b"host")
        if not values:
            # HTTP/1.0 and other non-browser clients; every browser sends Host.
            return True
        if len(values) > 1:
            return False
        name = host_header_name(values[0])
        return name is not None and (name in self.names or parse_ip(name) is not None)


def host_policy(config: AccessConfig, *, bind_host: str | None) -> HostPolicy:
    names = {"localhost", *machine_host_names(), *config.allow_hosts}
    origin = config.public_origin()
    if origin is not None:
        name = normalize_host_name(origin[1])
        if name is not None:
            names.add(name)
    bind = normalize_host_name(bind_host) if bind_host else None
    if bind is not None:
        names.add(bind)
    return HostPolicy(frozenset(names))


@functools.cache
def machine_host_names() -> frozenset[str]:
    """This computer's names: host name, fully qualified name and mDNS ``<name>.local``."""
    names: set[str] = set()
    for raw in (socket.gethostname(), _fully_qualified_name()):
        name = normalize_host_name(raw) if raw else None
        if name is None or parse_ip(name) is not None:
            continue
        short = name.split(".", 1)[0]
        names.update({name, short, f"{short}.local"})
    return frozenset(names)


def host_header_name(value: str) -> str | None:
    """The name a ``Host`` value carries, without its port; ``None`` when malformed."""
    text = value.strip()
    if text.startswith("["):
        end = text.find("]")
        rest = text[end + 1 :] if end > 0 else ""
        if end <= 0 or (rest and not (rest.startswith(":") and rest[1:].isdigit())):
            return None
        host = text[1:end]
        return host.casefold() if parse_ip(host) is not None and ":" in host else None
    host, separator, port = text.partition(":")
    if separator and not port.isdigit():
        return None
    return normalize_host_name(host) if host else None


def _fully_qualified_name() -> str:
    try:
        return socket.getfqdn()
    except OSError:
        return ""


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
    hosts: HostPolicy | None = None,
) -> RequestFacts:
    """Resolve the effective client and apply the mode's peer, transport and Host rules."""
    headers: list[tuple[bytes, bytes]] = list(scope.get("headers") or ())
    facts = _inspect_peer(scope, headers, config, admitted=admitted, proxies=proxies)
    if facts.refusal is None and hosts is not None and not hosts.allows(headers):
        return dataclasses.replace(facts, refusal="host_not_allowed")
    return facts


def request_host(scope: Mapping[str, Any]) -> str:
    """The request's ``Host`` value(s) for a log line, shortened."""
    values = _header_values(scope.get("headers") or (), b"host")
    text = ",".join(values) or "none"
    return text if len(text) <= 80 else f"{text[:80]}..."


def _inspect_peer(
    scope: Mapping[str, Any],
    headers: list[tuple[bytes, bytes]],
    config: AccessConfig,
    *,
    admitted: tuple[IpNetwork, ...] | None,
    proxies: tuple[IpNetwork, ...],
) -> RequestFacts:
    client_info = scope.get("client")
    peer = parse_ip(str(client_info[0])) if client_info else None
    proxy_headers = any(
        name in _PROXY_HEADERS or name.startswith(_PROXY_HEADER_PREFIX) for name, _value in headers
    )
    websocket = scope.get("type") == "websocket"
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
        client = _forwarded_client(_header_values(headers, b"x-forwarded-for"), proxies)
        if client is None:
            return RequestFacts(peer, None, True, True, False, "invalid_forwarded_for")
        protocols = _list_values(_header_values(headers, b"x-forwarded-proto"))
        # Proxies forward a WebSocket upgrade as https, some as wss.
        accepted = {"https", "wss"} if websocket else {"https"}
        forwarded_secure = bool(protocols) and protocols[-1].casefold() in accepted
        return RequestFacts(
            peer, client, True, True, forwarded_secure, None if forwarded_secure else "insecure"
        )
    # Plain HTTP only from this computer; everyone else needs HTTPS.
    refusal = None if secure or is_loopback_address(peer) else "insecure"
    return RequestFacts(peer, peer, proxy_headers, False, secure, refusal)


def is_capability_route(method: str, path: str) -> bool:
    """Whether this request shape belongs to a capability-verifying route."""
    return method in {"GET", "HEAD"} and _CAPABILITY_ROUTES.fullmatch(path) is not None


def is_webhook_route(method: str, path: str) -> bool:
    """Whether this request shape is a Cron webhook request."""
    return method == "POST" and _WEBHOOK_ROUTE.fullmatch(path) is not None


def classify_route(scope_type: str, method: str, path: str) -> RouteClass:
    """Whether a route needs a principal in ``authenticated`` mode. Fails closed."""
    if scope_type != "http":
        return "protected"
    if (method, path) in _EXEMPT_ROUTES or is_webhook_route(method, path):
        return "exempt"
    if method in {"GET", "HEAD"}:
        ticket = path.removeprefix(TICKET_PATH_PREFIX)
        if path.startswith(TICKET_PATH_PREFIX) and ticket and "/" not in ticket:
            return "exempt"
        if is_capability_route(method, path):
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


def _forwarded_client(values: list[str], proxies: tuple[IpNetwork, ...]) -> IpAddress | None:
    """The rightmost forwarded address that is not a trusted proxy.

    Addresses left of it were written by the client itself and prove nothing.
    A missing client, or any non-address entry, refuses the chain. A list of
    trusted proxies alone never establishes which client sent the request.
    """
    chain = _list_values(values)
    if not chain:
        return None
    parsed: list[IpAddress] = []
    for entry in chain:
        address = _forwarded_address(entry)
        if address is None:
            return None
        parsed.append(address)
    for address in reversed(parsed):
        if not address_in(address, proxies):
            return address
    return None


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
