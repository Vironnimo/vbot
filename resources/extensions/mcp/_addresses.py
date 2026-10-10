"""Which addresses an MCP connection may reach: the classes ``_network`` enforces.

Pure address arithmetic without the HTTP stack, so validating a connection
(``config``) needs no import of httpx2 or the MCP SDK.
"""

from __future__ import annotations

import ipaddress
from typing import Literal

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type Destination = Literal["forbidden", "private", "public"]

# Metadata services outside the link-local ranges.
_METADATA_ADDRESSES = frozenset(
    {ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}
)
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def address_of(value: str) -> IPAddress | None:
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
    address = address_of(name)
    return address is not None and address.is_loopback
