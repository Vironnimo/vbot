"""Where the HTTP requests of an MCP connection may go.

A connection's HTTP client sends the MCP traffic and, for OAuth, the discovery,
registration and token requests the SDK derives from metadata the server (or
its authorization server) supplies. Those URLs are not the user's choice, so
every request connects through ``DestinationGuard``: it resolves each host
itself and connects only to an address it checked, so neither a redirect nor a
later DNS answer can steer a connection to another address.

- Link-local addresses (cloud metadata services such as ``169.254.169.254``
  and ``fd00:ec2::254``), multicast, unspecified and reserved addresses are
  always refused.
- Loopback and private addresses are refused when the configured server is
  public, and allowed when every address of the configured server is itself
  loopback or private. That decision is made once per connection attempt.

Environment proxies are ignored (``trust_env=False``): through a proxy the
guard could not check the destination.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import Literal, override

import anyio
import httpcore2
import httpx2

from core.utils.tls import shared_ssl_context

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolver = Callable[[str, int], Awaitable[list[str]]]
type Destination = Literal["forbidden", "private", "public"]

# Metadata services outside the link-local ranges.
_METADATA_ADDRESSES = frozenset(
    {ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}
)
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
# httpx2's default pool limits.
_MAX_CONNECTIONS = 100
_MAX_KEEPALIVE_CONNECTIONS = 20
_KEEPALIVE_EXPIRY_SECONDS = 5.0


def _ip(value: str) -> IPAddress | None:
    """*value* as an address, IPv4 addresses embedded in IPv6 unwrapped; ``None`` for a name."""
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in _NAT64_PREFIX:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def classify(address: IPAddress) -> Destination:
    """Whether *address* is never reachable, only from a private server, or public."""
    if (
        address in _METADATA_ADDRESSES
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        return "forbidden"
    if address.is_loopback:
        return "private"
    if address.is_reserved:
        return "forbidden"
    return "public" if address.is_global else "private"


def is_loopback_host(host: str) -> bool:
    """Whether *host* names this machine: ``localhost`` or a loopback address."""
    name = host.strip("[]").rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    address = _ip(name)
    return address is not None and address.is_loopback


async def _resolve(host: str, port: int) -> list[str]:
    records = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(record[4][0]) for record in records))


class DestinationGuard(httpcore2.AsyncNetworkBackend):
    """A network backend that connects only to addresses the connection may reach."""

    def __init__(
        self,
        server_host: str,
        *,
        resolve: Resolver = _resolve,
        inner: httpcore2.AsyncNetworkBackend | None = None,
    ) -> None:
        self._server_host = server_host.strip("[]")
        self._resolve = resolve
        self._inner = inner or httpcore2.AnyIOBackend()
        self._private_server: bool | None = None

    async def _addresses(self, host: str, port: int) -> list[IPAddress]:
        literal = _ip(host)
        if literal is not None:
            return [literal]
        try:
            resolved = await self._resolve(host, port)
        except OSError as error:
            raise httpcore2.ConnectError(f"Cannot resolve {host}: {error}") from error
        addresses = [address for value in resolved if (address := _ip(value)) is not None]
        if not addresses:
            raise httpcore2.ConnectError(f"Cannot resolve {host}")
        return addresses

    async def _server_is_private(self, port: int) -> bool:
        if self._private_server is None:
            addresses = await self._addresses(self._server_host, port)
            self._private_server = all(classify(address) == "private" for address in addresses)
        return self._private_server

    async def permitted(self, host: str, port: int) -> list[str]:
        """The addresses of *host* this connection may connect to, or ``ConnectError``."""
        addresses = await self._addresses(host, port)
        private_server = await self._server_is_private(port)
        allowed: list[str] = []
        refusal = ""
        for address in addresses:
            destination = classify(address)
            if destination == "public" or (destination == "private" and private_server):
                allowed.append(str(address))
            elif not refusal:
                refusal = (
                    f"{address} is a link-local, metadata or reserved address"
                    if destination == "forbidden"
                    else f"{address} is a private network address and the MCP server is public"
                )
        if not allowed:
            raise httpcore2.ConnectError(f"MCP connection refused to reach {host}: {refusal}")
        return allowed

    @override
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        try:
            with anyio.fail_after(timeout):
                addresses = await self.permitted(host, port)
        except TimeoutError as error:
            raise httpcore2.ConnectTimeout(f"Resolving {host} timed out") from error
        failure: httpcore2.ConnectError | None = None
        for address in addresses:
            try:
                # TLS still verifies against *host*: httpcore passes the origin host
                # as the server name when it starts TLS on this stream.
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore2.ConnectError as error:
                failure = error
        assert failure is not None
        raise failure

    @override
    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        raise httpcore2.ConnectError("MCP connections do not use Unix sockets")

    @override
    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_transport(server_url: str) -> httpx2.AsyncHTTPTransport:
    """An HTTP transport for the connection to *server_url*, checked by ``DestinationGuard``."""
    transport = httpx2.AsyncHTTPTransport(verify=shared_ssl_context(), trust_env=False)
    host = httpx2.URL(server_url).host
    transport._pool = httpcore2.AsyncConnectionPool(  # noqa: SLF001 - httpx2 has no backend option.
        ssl_context=shared_ssl_context(),
        max_connections=_MAX_CONNECTIONS,
        max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
        keepalive_expiry=_KEEPALIVE_EXPIRY_SECONDS,
        network_backend=DestinationGuard(host),
    )
    return transport


def http_client(
    server_url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncClient:
    """The HTTP client of one connection to *server_url*."""
    return httpx2.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        trust_env=False,
        transport=guarded_transport(server_url),
    )
