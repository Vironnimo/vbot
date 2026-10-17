"""Access modes: which clients may reach the server, and over which transport.

The server runs in exactly one access mode, chosen by the host (server flags,
installation record, installer options) and never changeable through RPC:

- ``local``: loopback peers only; the server must bind a loopback address.
- ``lan``: loopback plus private networks (RFC 1918, link-local, IPv6 ULA) and
  the networks added with ``--allow-network``.
- ``authenticated``: anyone who can reach the server, with a credential on every
  protected request. It needs a public ``https://`` URL and either direct TLS
  (certificate and key) or at least one trusted reverse proxy.

This module validates and normalizes that configuration and answers the
address questions the server edge asks. Enforcement lives in ``server.access``.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, get_args
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import ssl

AccessMode = Literal["local", "lan", "authenticated"]
ACCESS_MODES: tuple[AccessMode, ...] = get_args(AccessMode)
DEFAULT_ACCESS_MODE: AccessMode = "local"

#: How user-facing text names each mode; the code names stay flag values.
ACCESS_MODE_LABELS: Mapping[AccessMode, str] = {
    "local": "This computer only",
    "lan": "Home network without login",
    "authenticated": "With login (internet)",
}

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

LOOPBACK_HOST = "127.0.0.1"
WILDCARD_HOST = "0.0.0.0"

LOOPBACK_NETWORKS: tuple[IpNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)
#: The networks ``lan`` admits besides loopback: RFC 1918, link-local and IPv6 ULA.
PRIVATE_NETWORKS: tuple[IpNetwork, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
)

_HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


class AccessConfigError(ValueError):
    """An access configuration the server refuses to start with."""


@dataclass(frozen=True)
class AccessConfig:
    """One validated, normalized access configuration.

    Build it with :func:`access_config` or :func:`access_config_from_record`;
    both enforce the mode rules. Networks are normalized CIDR strings, the public
    URL an ``https://host[:port]`` origin, and the TLS files absolute paths.
    """

    mode: AccessMode = DEFAULT_ACCESS_MODE
    allow_networks: tuple[str, ...] = ()
    public_url: str | None = None
    trusted_proxies: tuple[str, ...] = ()
    tls_cert: str | None = None
    tls_key: str | None = None

    @property
    def label(self) -> str:
        return ACCESS_MODE_LABELS[self.mode]

    @property
    def direct_tls(self) -> bool:
        """Whether the server terminates TLS itself."""
        return self.tls_cert is not None

    def admitted_networks(self) -> tuple[IpNetwork, ...] | None:
        """The client networks this mode admits; ``None`` admits every address."""
        if self.mode == "local":
            return LOOPBACK_NETWORKS
        if self.mode == "lan":
            extra = tuple(ipaddress.ip_network(value) for value in self.allow_networks)
            return (*LOOPBACK_NETWORKS, *PRIVATE_NETWORKS, *extra)
        return None

    def proxy_networks(self) -> tuple[IpNetwork, ...]:
        return tuple(ipaddress.ip_network(value) for value in self.trusted_proxies)

    def public_origin(self) -> tuple[str, str, int] | None:
        """The browser origin of the public URL as ``(scheme, host, port)``."""
        if self.public_url is None:
            return None
        parsed = urlsplit(self.public_url)
        assert parsed.hostname is not None
        return "https", parsed.hostname, parsed.port or 443

    def to_record(self) -> dict[str, Any]:
        """The installation record form; :func:`access_config_from_record` reads it."""
        return {
            "mode": self.mode,
            "allow_networks": list(self.allow_networks),
            "public_url": self.public_url,
            "trusted_proxies": list(self.trusted_proxies),
            "tls_cert": self.tls_cert,
            "tls_key": self.tls_key,
        }


def access_config(
    mode: str,
    *,
    allow_networks: Iterable[str] = (),
    public_url: str | None = None,
    trusted_proxies: Iterable[str] = (),
    tls_cert: str | Path | None = None,
    tls_key: str | Path | None = None,
) -> AccessConfig:
    """Validate one access configuration against its mode's rules.

    Raises :class:`AccessConfigError` with a message that names the offending
    option. TLS files are checked for presence here only; :func:`load_tls_context`
    loads them.
    """

    if mode not in ACCESS_MODES:
        raise AccessConfigError(
            f"Unknown access mode {mode!r}; choose one of {', '.join(ACCESS_MODES)}"
        )
    networks = _normalize_networks(allow_networks, "--allow-network")
    proxies = _normalize_networks(trusted_proxies, "--trusted-proxy")
    url = _normalize_public_url(public_url) if public_url not in (None, "") else None
    if (tls_cert in (None, "")) != (tls_key in (None, "")):
        raise AccessConfigError("--tls-cert and --tls-key must be given together")
    cert = _absolute_path(tls_cert) if tls_cert not in (None, "") else None
    key = _absolute_path(tls_key) if tls_key not in (None, "") else None
    if mode != "lan" and networks:
        raise AccessConfigError("--allow-network applies only to --access lan")
    if mode != "authenticated":
        for value, option in (
            (url, "--public-url"),
            (proxies, "--trusted-proxy"),
            (cert, "--tls-cert and --tls-key"),
        ):
            if value:
                raise AccessConfigError(f"{option} applies only to --access authenticated")
    else:
        if url is None:
            raise AccessConfigError(
                "--access authenticated needs --public-url https://... "
                "(the address browsers use to reach vBot)"
            )
        if cert is None and not proxies:
            raise AccessConfigError(
                "--access authenticated needs HTTPS: give --tls-cert and --tls-key, or "
                "--trusted-proxy with the address of the reverse proxy that terminates HTTPS"
            )
    return AccessConfig(
        mode=cast(AccessMode, mode),
        allow_networks=networks,
        public_url=url,
        trusted_proxies=proxies,
        tls_cert=cert,
        tls_key=key,
    )


def access_config_from_record(value: Any) -> AccessConfig:
    """Read the installation record form written by :meth:`AccessConfig.to_record`."""

    if not isinstance(value, Mapping):
        raise AccessConfigError("The recorded access configuration must be an object")
    allow = value.get("allow_networks", [])
    proxies = value.get("trusted_proxies", [])
    texts = (value.get("public_url"), value.get("tls_cert"), value.get("tls_key"))
    if (
        not isinstance(value.get("mode"), str)
        or not _is_string_list(allow)
        or not _is_string_list(proxies)
        or any(item is not None and not isinstance(item, str) for item in texts)
    ):
        raise AccessConfigError("The recorded access configuration is malformed")
    return access_config(
        value["mode"],
        allow_networks=allow,
        public_url=texts[0],
        trusted_proxies=proxies,
        tls_cert=texts[1],
        tls_key=texts[2],
    )


def default_host(config: AccessConfig) -> str:
    """The bind address a mode uses when no host is given."""
    if config.mode == "local" or (config.mode == "authenticated" and not config.direct_tls):
        return LOOPBACK_HOST
    return WILDCARD_HOST


def inferred_access_mode(host: str) -> AccessMode:
    """The mode of a server started without an access mode: by its bind address.

    Launchers that predate access modes pass only ``--host``. A loopback bind
    keeps serving this computer only; any other bind serves the home network
    without login, never the internet.
    """
    return "local" if is_loopback_host(host) else "lan"


def check_bind_host(config: AccessConfig, host: str) -> None:
    """Refuse a bind address the mode does not allow."""
    if config.mode == "local" and not is_loopback_host(host):
        raise AccessConfigError(
            f"--access local serves this computer only and must bind a loopback address "
            f"such as 127.0.0.1, not {host}; use --access lan for the home network"
        )


def is_loopback_host(host: str) -> bool:
    """Whether a bind or connect host names this computer's loopback interface."""
    text = host.strip().removeprefix("[").removesuffix("]")
    if text.casefold() == "localhost":
        return True
    address = parse_ip(text)
    return address is not None and is_loopback_address(address)


def parse_ip(value: str) -> IpAddress | None:
    """Parse an IP address, unwrapping IPv4-mapped IPv6 (``::ffff:a.b.c.d``)."""
    try:
        address = ipaddress.ip_address(value.strip().removeprefix("[").removesuffix("]"))
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address.scope_id is not None:
            # A zone (fe80::1%eth0) names the interface, not another address.
            return ipaddress.IPv6Address(str(address).split("%", 1)[0])
    return address


def is_loopback_address(address: IpAddress) -> bool:
    return address_in(address, LOOPBACK_NETWORKS)


def address_in(address: IpAddress, networks: Iterable[IpNetwork]) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def load_tls_context(cert: str, key: str) -> ssl.SSLContext:
    """Load the server certificate chain and key into a server TLS context."""
    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert, key)
    except FileNotFoundError as exc:
        raise AccessConfigError(
            f"The TLS certificate or key file does not exist: {exc.filename or cert}"
        ) from exc
    except (OSError, ssl.SSLError) as exc:
        raise AccessConfigError(
            f"The TLS certificate {cert} and key {key} could not be loaded: {exc}"
        ) from exc
    return context


def _normalize_networks(values: Iterable[str], option: str) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    found: list[str] = []
    for raw in values:
        text = str(raw).strip()
        try:
            network = ipaddress.ip_network(text, strict=True)
        except ValueError as exc:
            raise AccessConfigError(
                f"{option} needs an IP address or a CIDR network such as 192.168.1.0/24, "
                f"not {raw!r}"
            ) from exc
        if network.prefixlen == 0:
            raise AccessConfigError(
                f"{option} {text} would cover every address; name the actual network"
            )
        normalized = str(network)
        if normalized not in found:
            found.append(normalized)
    return tuple(found)


def _normalize_public_url(value: str | None) -> str:
    text = (value or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise AccessConfigError(f"--public-url {value!r} is not a valid URL") from exc
    if parsed.scheme.casefold() != "https":
        raise AccessConfigError("--public-url must start with https://")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        raise AccessConfigError(
            "--public-url must be an origin such as https://vbot.example.org, "
            "without path, query or credentials"
        )
    host = parsed.hostname.casefold()
    if parse_ip(host) is None and not all(
        _HOST_LABEL.fullmatch(label) for label in host.removesuffix(".").split(".")
    ):
        raise AccessConfigError(f"--public-url names an invalid host: {host}")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    return f"https://{netloc}"


def _absolute_path(value: str | Path | None) -> str:
    return str(Path(str(value)).expanduser().resolve())


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
