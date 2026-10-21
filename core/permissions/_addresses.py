"""IPv4 addresses in every form the C resolver accepts, so checks compare one form.

``inet_aton`` reads ``127.1``, ``2130706433``, ``0x7f000001`` and ``0177.0.0.1``
as ``127.0.0.1``; curl, browsers and most programs pass host names through it.
"""

from __future__ import annotations

import ipaddress
import re

_PART = re.compile(r"^(?:0[xX][0-9A-Fa-f]*|[0-9]+)$")


def parse_ipv4(text: str) -> ipaddress.IPv4Address | None:
    """The address *text* names in ``inet_aton`` syntax (1 to 4 parts), else None.

    Each part is decimal, ``0x`` hexadecimal or ``0`` octal; the last part
    fills the remaining bytes.
    """
    parts = text.split(".")
    if not 1 <= len(parts) <= 4 or not all(_PART.match(part) for part in parts):
        return None
    values: list[int] = []
    for part in parts:
        try:
            if part[:2].lower() == "0x":
                values.append(int(part[2:] or "0", 16))
            elif len(part) > 1 and part[0] == "0":
                values.append(int(part[1:], 8))
            else:
                values.append(int(part))
        except ValueError:
            return None  # 08: not octal
    *head, last = values
    if any(value > 255 for value in head) or last >= 256 ** (4 - len(head)):
        return None
    number = 0
    for value in head:
        number = number * 256 + value
    return ipaddress.IPv4Address((number << (8 * (4 - len(head)))) | last)


def canonical_host(host: str) -> str:
    """*host* with an IPv4 address in its dotted form (``127.1`` -> ``127.0.0.1``)."""
    address = parse_ipv4(host.rstrip("."))
    return str(address) if address is not None else host
