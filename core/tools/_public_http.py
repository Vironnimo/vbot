"""Guarded HTTP GET for public internet addresses.

web_fetch reads pages with it and analyze_image downloads image URLs with it. The
guard is the same for every caller and is safety, not permission: only http(s)
URLs without credentials, only public addresses (the resolved address is pinned
for the connection, so DNS rebinding cannot swap in a private one), every
redirect hop revalidated, a bounded body and bounded retries. Failures carry
precise codes and wording that say what went wrong and what to try next. A
restricted caller passes its Permission Boundary: every hop is checked against
it too (``check_url``) and a refused one raises ``PermissionDenied``.

curl_cffi loads with the first request, in a Tool worker: importing it (with
markdownify, bs4 and charset_normalizer, which it imports) costs 0.3-0.4 s, which
every process that imports the Tools would otherwise pay, and which would block
the Event Loop where the request starts.
"""

from __future__ import annotations

import asyncio
import codecs
import html
import importlib
import ipaddress
import re
import socket
import time
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal
from urllib.parse import urljoin, urlparse

from core.permissions import (
    PermissionBoundary,
    PermissionDenied,
    PermissionGrant,
    PermissionRefusal,
    normalize_host,
)
from core.tools.tools import JsonObject, run_tool_worker, tool_failure
from core.utils.http_status import is_retryable_status, parse_retry_after
from core.utils.logging import get_logger
from core.utils.retry import MAX_RETRIES, sleep_for_retry

if TYPE_CHECKING:
    from curl_cffi.requests import AsyncSession
    from curl_cffi.requests.exceptions import RequestException

_LOGGER = get_logger("tools.public_http")

MAX_RESPONSE_BYTES = 50 * 1024 * 1024

# Impersonate a recent real Chrome so the TLS/HTTP-2 fingerprint matches a
# browser. Header-only spoofing does not fool fingerprint-based bot walls
# (Cloudflare, Akamai, DataDome) — the request has to *look* like Chrome at the
# transport layer, which is exactly what curl_cffi's impersonation provides.
# "chrome" is a rolling alias for the newest profile, so it stays current
# without pinning a version (pinning would silently go stale).
_IMPERSONATE_TARGET: Literal["chrome"] = "chrome"

_CONNECT_TIMEOUT_SECONDS = 5.0
_TOTAL_TIMEOUT_SECONDS = 30.0
_REQUEST_TIMEOUT = (_CONNECT_TIMEOUT_SECONDS, _TOTAL_TIMEOUT_SECONDS)
_MAX_REDIRECTS = 10
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
# Requests to one host at once. A burst of parallel fetches to one host made its
# firewall block this machine for about 10 minutes (Swarm run 2026-10-20: about 40
# within 10 s); browsers open at most 6 connections per host.
HOST_CONCURRENCY = 4
# Titles of check pages that solve themselves in a real browser. The local browser
# waits while one shows, and a client error showing one is a bot check, which the
# browser can pass, rather than a refusal of the address.
SELF_SOLVING_CHECK_TITLES = frozenset(
    {
        "just a moment...",
        "checking your browser...",
        "checking your browser before accessing",
        "please wait...",
        "one moment, please...",
        "ddos-guard",
        "attention required! | cloudflare",
        "enodia verification",
    }
)
_PAGE_TITLE = re.compile(r"<title[^>]*>([^<]*)</title", re.I)

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class PublicResponse:
    """Normalized outcome of a single GET, decoupled from the HTTP library.

    Header keys are lower-cased so lookups (``content-type``, ``location``) are
    case-insensitive. ``content`` holds the raw response bytes (needed to sniff
    and store images); ``text`` is the decoded body used for the text paths.
    """

    status_code: int
    headers: dict[str, str]
    text: str
    url: str
    content: bytes = b""
    attempts: int = 1


_cooldown_clock = time.monotonic
_cooldown_sleep = asyncio.sleep
_MAX_INLINE_COOLDOWN = 15.0
_FIREWALL_COOLDOWN = 300.0


@dataclass
class _HostPause:
    until: float
    concurrency: int = 1
    active: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def wake(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()


class HostSlots:
    """Limit concurrent direct requests and coordinate recovery across calls to one host."""

    def __init__(self, per_host: int = HOST_CONCURRENCY) -> None:
        self._per_host = per_host
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._users: Counter[str] = Counter()
        self._paused: OrderedDict[str, _HostPause] = OrderedDict()
        self._addresses: OrderedDict[tuple[str, int], tuple[str, float]] = OrderedDict()

    def address_order(self, target: tuple[str, int], current: tuple[str, ...]) -> tuple[str, ...]:
        preferred = self._addresses.get(target)
        if preferred is None:
            return current
        address, until = preferred
        if _cooldown_clock() >= until or address not in current:
            del self._addresses[target]
            return current
        return (address, *(other for other in current if other != address))

    def connected(self, target: tuple[str, int], address: str) -> None:
        self._addresses[target] = (address, _cooldown_clock() + 300)
        self._addresses.move_to_end(target)
        if len(self._addresses) > 1024:
            self._addresses.popitem(last=False)

    def is_paused(self, host: str) -> bool:
        """Whether the host's deadline still rules out an external fetch."""
        pause = self._paused.get(host)
        return pause is not None and _cooldown_clock() < pause.until

    def needs_probe(self, host: str) -> bool:
        return host in self._paused

    def firewall_blocked(self, host: str) -> None:
        self.pause(host, _FIREWALL_COOLDOWN)

    def pause(self, host: str, seconds: float) -> None:
        until = _cooldown_clock() + seconds
        previous = self._paused.get(host)
        if previous is not None and previous.until > until:
            until = previous.until
        if previous is not None:
            previous.wake()
        self._paused[host] = _HostPause(until)
        self._paused.move_to_end(host)
        if len(self._paused) > 1024:
            _, forgotten = self._paused.popitem(last=False)
            forgotten.wake()

    def answered(self, host: str, probe: _HostPause | None, status: int) -> None:
        if probe is None or self._paused.get(host) is not probe:
            return
        if 200 <= status < 400:
            # Each confirmed response admits one more concurrent probe.
            probe.concurrency += 1
            if probe.concurrency >= self._per_host:
                del self._paused[host]
        else:
            # This page did not establish that normal traffic can resume.
            probe.concurrency = 1
        probe.wake()

    @asynccontextmanager
    async def hold(self, host: str) -> AsyncIterator[_HostPause | None]:
        semaphore = self._semaphores.get(host)
        if semaphore is None:
            semaphore = self._semaphores[host] = asyncio.Semaphore(self._per_host)
        self._users[host] += 1
        try:
            async with semaphore:
                while (pause := self._paused.get(host)) is not None:
                    remaining = pause.until - _cooldown_clock()
                    if remaining > _MAX_INLINE_COOLDOWN:
                        raise PublicFetchError(
                            "rate_limited",
                            f"{host} is limiting requests. Try another source or wait before "
                            "fetching from this host again.",
                            retryable=True,
                            retry_after=remaining,
                        )
                    if pause.active >= pause.concurrency:
                        await pause.changed.wait()
                        continue
                    pause.active += 1
                    try:
                        if remaining > 0:
                            await _cooldown_sleep(remaining)
                        # An overlapping response may have extended the pause.
                        if self._paused.get(host) is not pause:
                            continue
                        yield pause
                        return
                    except Exception:
                        pause.concurrency = 1
                        raise
                    finally:
                        pause.active -= 1
                        pause.wake()
                yield None
        finally:
            self._users[host] -= 1
            if not self._users[host]:
                del self._users[host], self._semaphores[host]


class PublicFetchError(Exception):
    """A public address could not be fetched; the message says why and what next.

    ``recoverable`` tells whether a different fetcher (such as a configured fetch
    service) may still succeed: true for refusals by the site or the network,
    false for addresses the guard refuses and for pages that do not exist.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        attempts_made: int | None = None,
        recoverable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts_made = attempts_made
        self.recoverable = recoverable
        self.retry_after = retry_after

    def failure(self) -> JsonObject:
        """Return the Tool failure envelope for this error."""
        return tool_failure(
            self.code, str(self), retryable=self.retryable, attempts_made=self.attempts_made
        )


class _RedirectLimitExceededError(Exception):
    """Raised when a redirect chain loops or exceeds ``_MAX_REDIRECTS`` hops."""


class _RedirectTargetBlockedError(Exception):
    """Raised when a server redirect targets a blocked (private/non-http) address.

    The caller's URL was valid; the server chose the redirect target, so this is
    reported as a blocked address rather than as an argument problem.
    """


class _RedirectTargetUnreachableError(Exception):
    """Raised when a server redirects to a host name that does not resolve.

    Bot checks do this (a page redirects automated requests to a challenge host
    that does not exist for them); the caller did not write that address.
    """


class _ResponseTooLargeError(Exception):
    """Raised when a fetched response exceeds the bounded in-memory transfer limit."""


class _InvalidUrlError(ValueError):
    """The address is not a fetchable http(s) URL."""


class _BlockedUrlError(ValueError):
    """The address is valid but the public-URL policy refuses it."""


class _HostNotFoundError(ValueError):
    """The host name does not resolve."""


class _TransportFailedError(Exception):
    """A request to ``url`` (a redirect hop or the address asked for) failed below
    HTTP after the retries its kind allows."""

    def __init__(self, error: RequestException, attempts: int, url: str) -> None:
        self.error = error
        self.attempts = attempts
        self.url = url
        super().__init__(str(error))


_curl_cffi_loaded = False


async def _load_curl_cffi() -> None:
    """Import curl_cffi in a Tool worker once, before the first request uses it.

    Later imports of its modules, on the Event Loop too, then only look them up.
    Concurrent first requests each wait in a worker for the one import.
    """
    global _curl_cffi_loaded
    if not _curl_cffi_loaded:
        await run_tool_worker(importlib.import_module, "curl_cffi.requests")
        _curl_cffi_loaded = True


def _make_session(accept: str | None) -> AsyncSession:
    """Create a browser-impersonating session with an automatic cookie jar.

    ``impersonate`` gives every request a real Chrome TLS/HTTP-2 fingerprint,
    which is what gets past the fingerprint-based bot walls (Cloudflare, Akamai,
    DataDome) that reject a plain HTTP client no matter how browser-like its
    headers are. The session also keeps cookies across redirect hops, so a
    challenge cookie set on one hop is presented on the next. The connect target
    of each hop is pinned to a pre-validated public IP per request via
    ``CurlOpt.RESOLVE`` (see ``_fetch_with_retry``), so a DNS-rebinding answer
    cannot swap in a private address between validation and connection while the
    hostname still drives the Host header and TLS SNI / certificate check.
    ``accept`` ``None`` keeps the impersonated browser's own Accept header.
    """
    from curl_cffi.requests import AsyncSession

    headers = {} if accept is None else {"Accept": accept}
    # A proxy resolving the destination itself would bypass the validated DNS pins.
    return AsyncSession(impersonate=_IMPERSONATE_TARGET, headers=headers, proxies={"all": ""})


def _article(what: str) -> str:
    return f"{'an' if what[:1] in 'aeiou' else 'a'} {what}"


def _size_label(max_bytes: int) -> str:
    megabytes, remainder = divmod(max_bytes, 1024 * 1024)
    return f"{megabytes} MB" if megabytes and not remainder else f"{max_bytes:,} bytes"


async def _http_get(
    session: AsyncSession, url: str, max_bytes: int, *, truncate: bool = False
) -> PublicResponse:
    """Perform a bounded GET with redirects disabled — the patchable network seam.

    Tests substitute this coroutine to feed canned responses without touching
    the network; production streams into one bounded byte buffer so a missing or
    dishonest ``Content-Length`` cannot make the process retain an unlimited body.
    A longer body is refused, or with ``truncate`` cut at ``max_bytes``.
    """
    too_large = f"response exceeds the {_size_label(max_bytes)} download limit"
    async with session.stream(
        "GET",
        url,
        allow_redirects=False,
        timeout=_REQUEST_TIMEOUT,
    ) as response:
        headers = {name.lower(): value for name, value in response.headers.items()}
        declared_size = _declared_response_size(headers)
        if not truncate and declared_size is not None and declared_size > max_bytes:
            raise _ResponseTooLargeError(too_large)

        body = bytearray()
        async for chunk in response.aiter_content():
            if len(body) + len(chunk) > max_bytes:
                if not truncate:
                    raise _ResponseTooLargeError(too_large)
                body.extend(chunk[: max_bytes - len(body)])
                break
            body.extend(chunk)

        content = bytes(body)
        return PublicResponse(
            status_code=response.status_code,
            headers=headers,
            text=_decoded(content, headers.get("content-type", "")),
            url=str(response.url),
            content=content,
        )


_BYTE_ORDER_MARKS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_HEADER_CHARSET = re.compile(r"""charset\s*=\s*["']?([^"';\s]+)""", re.I)
_XML_ENCODING = re.compile(rb"""<\?xml[^>]*?encoding\s*=\s*["']([\w.:-]+)""")
_META_CHARSET = re.compile(rb"""<meta[^>]+?charset\s*=\s*["']?\s*([\w.:-]+)""", re.I)
# Text codecs that are no charset of a page, as browsers ignore them: they fail
# on ordinary bytes (``idna``, ``undefined``), rewrite them (the escape codecs,
# ``punycode``, ``utf-7``) or depend on the host (``mbcs``, ``oem``).
_NOT_CHARSETS = frozenset(
    {
        "idna",
        "undefined",
        "unicode-escape",
        "raw-unicode-escape",
        "punycode",
        "utf-7",
        "mbcs",
        "oem",
    }
)


def _decoded(content: bytes, content_type: str) -> str:
    """Decode a response body the way a browser would.

    A byte order mark wins, then the charset of the Content-Type header, then for
    markup a charset declared in its first 4 KB (XML declaration, ``<meta>``),
    then UTF-8 if the bytes are valid UTF-8, else Windows-1252. Pages without a
    header charset that declare Latin-1 in a ``<meta>`` (German law portals, for
    example) otherwise lose every umlaut to U+FFFD.
    """
    for mark, marked in _BYTE_ORDER_MARKS:
        if content.startswith(mark):
            return content.decode(marked, errors="replace")
    declared = _HEADER_CHARSET.search(content_type)
    codec = _codec(declared[1], in_body=False) if declared else None
    media_type = content_type.split(";", 1)[0].strip().lower()
    head = content[:4096].lstrip()
    if codec is None and ("html" in media_type or "xml" in media_type or head[:1] == b"<"):
        found = _XML_ENCODING.match(head) or _META_CHARSET.search(head)
        codec = _codec(found[1].decode("ascii"), in_body=True) if found else None
    if codec is not None:
        return content.decode(codec, errors="replace")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def _codec(label: str, *, in_body: bool) -> str | None:
    """Return the Python codec for a charset label, as browsers read the label."""
    try:
        name = codecs.lookup(label).name
        b"".decode(name)  # refuses codecs that do not decode text, such as base64
    except LookupError, ValueError:
        return None
    if name in _NOT_CHARSETS:
        return None
    if name in {"iso8859-1", "ascii"}:
        # Browsers decode both labels as Windows-1252, which pages labelled
        # Latin-1 use for characters such as the euro sign and typographic quotes.
        return "cp1252"
    if in_body and name.startswith(("utf-16", "utf-32")):
        # A page that could declare this in readable ASCII is not UTF-16 or UTF-32.
        return "utf-8"
    return name


def _declared_response_size(headers: dict[str, str]) -> int | None:
    """Return a valid declared body size, when the server sent one."""
    value = headers.get("content-length")
    if value is None:
        return None
    try:
        size = int(value)
    except ValueError:
        return None
    return size if size >= 0 else None


def _default_port_for_scheme(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _parse_ipv4_component(value: str) -> int | None:
    if not value:
        return None
    if value.startswith(("+", "-")):
        return None

    if value.lower().startswith("0x"):
        digits = value[2:]
        if not digits:
            return None
        base = 16
    elif len(value) > 1 and value.startswith("0"):
        digits = value[1:]
        if not digits:
            return 0
        base = 8
    else:
        digits = value
        base = 10

    try:
        return int(digits, base)
    except ValueError:
        return None


def _parse_obfuscated_ipv4(host: str) -> ipaddress.IPv4Address | None:
    parts = host.split(".")

    if len(parts) == 1:
        value = _parse_ipv4_component(parts[0])
        if value is None or value > 0xFFFFFFFF:
            return None
        return ipaddress.IPv4Address(value)

    if len(parts) < 2 or len(parts) > 4:
        return None

    parsed_parts: list[int] = []
    for part in parts:
        value = _parse_ipv4_component(part)
        if value is None:
            return None
        parsed_parts.append(value)

    if len(parsed_parts) == 2:
        first, second = parsed_parts
        if first > 0xFF or second > 0xFFFFFF:
            return None
        packed = (first << 24) | second
        return ipaddress.IPv4Address(packed)

    if len(parsed_parts) == 3:
        first, second, third = parsed_parts
        if first > 0xFF or second > 0xFF or third > 0xFFFF:
            return None
        packed = (first << 24) | (second << 16) | third
        return ipaddress.IPv4Address(packed)

    first, second, third, fourth = parsed_parts
    if first > 0xFF or second > 0xFF or third > 0xFF or fourth > 0xFF:
        return None
    return ipaddress.IPv4Address((first << 24) | (second << 16) | (third << 8) | fourth)


def _parse_ip_literal(host: str) -> IpAddress | None:
    host_without_zone = host.split("%", 1)[0]

    try:
        return ipaddress.ip_address(host_without_zone)
    except ValueError:
        pass

    return _parse_obfuscated_ipv4(host_without_zone)


def _is_blocked_ip(address: IpAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_blocked_ip(address.ipv4_mapped)

    return (
        not address.is_global
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def _resolve_host_addresses(host: str, port: int) -> list[IpAddress]:
    loop = asyncio.get_running_loop()

    try:
        info = await loop.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as error:
        raise _HostNotFoundError(_host_not_found(host)) from error

    addresses: list[IpAddress] = []
    seen: set[str] = set()
    for family, _, _, _, socket_address in info:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue

        address_text = str(socket_address[0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError:
            continue

        key = str(address)
        if key in seen:
            continue
        seen.add(key)
        addresses.append(address)

    if not addresses:
        raise _HostNotFoundError(_host_not_found(host))

    return addresses


def _host_not_found(host: str) -> str:
    return f'Host "{host}" was not found (DNS lookup failed). Check the address for typos.'


def _private_address(host: str) -> str:
    return (
        f"{host} is a private or local network address; only public internet addresses "
        "can be fetched."
    )


async def _validate_public_target(
    scheme: str, host: str | None, port: int
) -> tuple[str, tuple[str, ...]]:
    """Validate the entire DNS answer before any address can be pinned to a request."""
    if not scheme:
        raise _InvalidUrlError(
            "This is not a web address. Pass a full URL such as https://example.com/page."
        )
    if scheme not in {"http", "https"}:
        raise _InvalidUrlError(f"Only http and https URLs can be fetched, not {scheme}: URLs.")

    if not (host or "").strip().rstrip("."):
        raise _InvalidUrlError(
            "The URL has no host name. Pass a full URL such as https://example.com/page."
        )
    # The form the permission rules compared; a name it has none for is refused.
    normalized_host = normalize_host(host or "")
    if normalized_host is None:
        visible = "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in host or "")
        raise _InvalidUrlError(
            f'"{visible}" is not a valid host name. Check the address for typos and '
            "invisible characters."
        )

    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise _BlockedUrlError(_private_address(normalized_host))

    literal_address = _parse_ip_literal(normalized_host)
    if literal_address is not None:
        if _is_blocked_ip(literal_address):
            # Name the address as the URL wrote it; an IPv4-mapped one compares as IPv4.
            raise _BlockedUrlError(_private_address((host or "").strip().strip("[]").lower()))
        return str(literal_address), (str(literal_address),)

    resolved_addresses = await _resolve_host_addresses(normalized_host, port)
    for resolved in resolved_addresses:
        if _is_blocked_ip(resolved):
            raise _BlockedUrlError(_private_address(normalized_host))
    return normalized_host, tuple(map(str, resolved_addresses))


_CREDENTIALS_BLOCKED = (
    "URLs containing a user name or password cannot be fetched; remove the credentials "
    "from the address."
)


def _url_parts(url: str) -> tuple[str, str | None, int]:
    """Split a URL for validation, reporting malformed ones as invalid URLs."""
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            raise _BlockedUrlError(_CREDENTIALS_BLOCKED)
        return (
            parsed.scheme,
            parsed.hostname,
            parsed.port or _default_port_for_scheme(parsed.scheme),
        )
    except _BlockedUrlError:
        raise
    except ValueError as error:
        raise _InvalidUrlError(
            f"The URL is malformed ({error}). Pass a full URL such as https://example.com/page."
        ) from error


# curl reports a connection the peer cut off during the TLS handshake as a TLS
# failure; only its reason shows the cut ("Recv failure: Connection was reset",
# "SSL_ERROR_SYSCALL", "unexpected eof while reading").
_CUT_OFF_HANDSHAKE = re.compile(
    r"\b(?:reset|aborted|broken pipe|closed|eof|unexpected_eof|recv failure|send failure"
    r"|ssl_error_syscall)\b",
    re.IGNORECASE,
)


def _is_tls_failure(error: RequestException) -> bool:
    """Whether the site's certificate or TLS setup failed, which repeats on every attempt.

    A handshake the peer cut off is a connection failure instead: the same page
    often loads seconds later.
    """
    from curl_cffi.requests.exceptions import CertificateVerifyError, SSLError

    if isinstance(error, CertificateVerifyError):
        return True
    return isinstance(error, SSLError) and not _CUT_OFF_HANDSHAKE.search(_curl_detail(error))


def _retries_allowed(error: RequestException) -> int:
    """How often a transport failure is worth repeating.

    A broken certificate or TLS setup fails the same way every time. A timeout
    during connection or response transfer gets one more bounded attempt.
    """
    from curl_cffi.requests.exceptions import DNSError, Timeout

    if isinstance(error, DNSError) or _is_tls_failure(error):
        return 0
    if isinstance(error, Timeout):
        return 1
    return MAX_RETRIES


def _global_rate_limit(response: PublicResponse) -> bool:
    return (
        response.status_code == 429
        and response.headers.get("x-ratelimit-global", "").strip().lower() == "true"
    )


def _is_firewall_page(response: PublicResponse) -> bool:
    from core.tools._web_fetch_html import extract_views
    from core.tools._web_fetch_walls import firewall_block

    # Only the known block-page title warrants an HTML parse in this transport path.
    main, _page, metadata, _warnings = extract_views(
        response.text, response.url, include_links=False
    )
    return firewall_block(response.url, metadata, main) is not None


async def _request_with_retry(
    session: AsyncSession,
    url: str,
    max_bytes: int,
    *,
    addresses: tuple[str, ...],
    target: tuple[str, int],
    resolve_map: dict[tuple[str, int], str],
    truncate: bool = False,
    hosts: HostSlots | None = None,
) -> PublicResponse:
    """Fetch a URL and retry transient transport/status failures with backoff."""
    from curl_cffi import CurlECode, CurlOpt
    from curl_cffi.requests.exceptions import RequestException

    host, _ = target
    addresses = hosts.address_order(target, addresses) if hosts else addresses
    index = 0
    can_switch = True
    literal = _parse_ip_literal(host) is not None
    for attempt in range(MAX_RETRIES + 1):
        try:
            slot: AbstractAsyncContextManager[_HostPause | None] = (
                hosts.hold(host) if hosts is not None else nullcontext()
            )
            async with slot as probe:
                if not literal:
                    previous = resolve_map.get(target)
                    resolve_map[target] = addresses[index]
                    session.curl_options[CurlOpt.FRESH_CONNECT] = int(  # type: ignore[assignment]
                        previous is not None and previous != addresses[index]
                    )
                # curl's slist option accepts a list despite its narrower type stub.
                session.curl_options[CurlOpt.RESOLVE] = [  # type: ignore[assignment]
                    f"{name}:{port}:{_curl_resolve_address(ip)}"
                    for (name, port), ip in resolve_map.items()
                ]
                result = await _http_get(session, url, max_bytes, truncate=truncate)
                can_switch = False
                if hosts is not None and _global_rate_limit(result):
                    hint = parse_retry_after(result.headers)
                    delay = (
                        hint if hint is not None else (2**attempt if attempt < MAX_RETRIES else 30)
                    )
                    hosts.pause(host, delay)
                    if delay > _MAX_INLINE_COOLDOWN or attempt == MAX_RETRIES:
                        return replace(result, attempts=attempt + 1)
                    continue
                if (
                    hosts is not None
                    and result.status_code == 200
                    and "request rejected" in result.text[:8192].lower()
                    and await run_tool_worker(_is_firewall_page, result)
                ):
                    hosts.firewall_blocked(host)
                elif hosts is not None:
                    hosts.answered(host, probe, result.status_code)
                    if index and 200 <= result.status_code < 400:
                        hosts.connected(target, addresses[index])
        except RequestException as error:
            if attempt >= _retries_allowed(error):
                raise _TransportFailedError(error, attempt + 1, url) from error
            if (
                can_switch
                and error.code == CurlECode.COULDNT_CONNECT
                and not getattr(error.response, "status_code", 0)
                and index + 1 < len(addresses)
            ):
                index += 1
                continue
            # Once delivery is ambiguous, subsequent retries stay on this address.
            can_switch = False
            await sleep_for_retry(attempt)
            continue
        # GET is idempotent — safe to repeat (includes a transient 500).
        if (
            result.status_code >= 400
            and attempt < MAX_RETRIES
            and is_retryable_status(result.status_code, idempotent=True)
        ):
            await sleep_for_retry(attempt, parse_retry_after(result.headers))
            continue

        return replace(result, attempts=attempt + 1)

    raise RuntimeError("unreachable retry loop state")


async def _fetch_with_retry(
    session: AsyncSession,
    url: str,
    resolve_map: dict[tuple[str, int], str],
    max_bytes: int = MAX_RESPONSE_BYTES,
    what: str = "page",
    hosts: HostSlots | None = None,
    *,
    permissions: PermissionBoundary | PermissionGrant,
    truncate: bool = False,
) -> PublicResponse:
    """Fetch a URL, validating each redirect hop against SSRF rules and *permissions*.

    Each hop's host is pinned to the IP that just cleared validation via curl's
    ``RESOLVE`` map, so the session connects to exactly the address that was
    checked while the hostname still drives Host header and TLS verification.
    ``resolve_map`` accumulates ``(host, port) -> ip`` across hops so an earlier
    hop's pin survives a same-host redirect.
    """
    current_url = url
    # The cookies held when each address was requested. A login or consent
    # service sets cookies and sends the request back to the address it came
    # from (nature.com and link.springer.com articles return through their
    # identity service), which then serves the page; the same address with the
    # same cookies answers the same way again, so that is a loop.
    visited: dict[str, frozenset[tuple[str, str, str, str | None]]] = {}

    for redirect_count in range(_MAX_REDIRECTS + 1):
        cookies = _cookie_state(session)
        if visited.get(current_url) == cookies:
            raise _RedirectLimitExceededError(
                f"{url} redirects in a loop and never reaches {_article(what)}, also with "
                "the cookies it sets. Try another source."
            )
        visited[current_url] = cookies
        _check_hop(permissions, url, current_url)
        try:
            scheme, host, port = _url_parts(current_url)
            normalized_host, addresses = await _validate_public_target(scheme, host, port)
        except _HostNotFoundError as error:
            if redirect_count > 0:
                raise _RedirectTargetUnreachableError(
                    f"{url} redirected to {host}, which could not be found (DNS lookup "
                    f"failed), so {_article(what)} cannot be reached there. Sites send "
                    "automated requests to such addresses to turn them away. Try another "
                    "source."
                ) from error
            raise
        except ValueError as error:
            if redirect_count > 0:
                raise _RedirectTargetBlockedError(
                    f"{url} redirected to an address that is not followed. {error}"
                ) from error
            raise
        # curl must parse exactly the host that was validated, including IDNA
        # and trailing-dot normalization. Otherwise RESOLVE misses its cache key
        # and curl performs a second, unchecked DNS lookup.
        parsed = urlparse(current_url)
        authority = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
        if parsed.port is not None:
            authority = f"{authority}:{port}"
        current_url = parsed._replace(netloc=authority).geturl()
        result = await _request_with_retry(
            session,
            current_url,
            max_bytes,
            addresses=addresses,
            target=(normalized_host, port),
            resolve_map=resolve_map,
            truncate=truncate,
            hosts=hosts,
        )
        if result.status_code not in _REDIRECT_STATUS_CODES:
            return result

        location = result.headers.get("location")
        if not location:
            return result

        if redirect_count >= _MAX_REDIRECTS:
            raise _RedirectLimitExceededError(
                f"{url} redirected more than {_MAX_REDIRECTS} times without reaching "
                f"{_article(what)}. Try another source or a more direct address."
            )

        current_url = urljoin(current_url, location)

    raise RuntimeError("unreachable retry loop state")


def hop_refusal(
    permissions: PermissionBoundary | PermissionGrant, url: str, hop: str
) -> PermissionRefusal | None:
    """Whether *permissions* refuse *hop*, an address a fetch of *url* reached.

    A refused hop other than *url* (a redirect, or the page a service or the
    browser ended on) names the address asked for and the host it led to.
    """
    refused = permissions.check_url(hop)
    if refused is not None and hop != url:
        refused = replace(refused, target=url, resolved=refused.target)
    return refused


def _check_hop(permissions: PermissionBoundary | PermissionGrant, url: str, hop: str) -> None:
    """Raise ``PermissionDenied`` when *permissions* refuse *hop* of a fetch of *url*."""
    refused = hop_refusal(permissions, url, hop)
    if refused is not None:
        raise PermissionDenied(refused)


def _cookie_state(session: AsyncSession) -> frozenset[tuple[str, str, str, str | None]]:
    """Return the cookies a session holds, to tell a revisit after a login hop from a loop."""
    jar = getattr(getattr(session, "cookies", None), "jar", ())
    return frozenset((cookie.domain, cookie.path, cookie.name, cookie.value) for cookie in jar)


def _curl_resolve_address(address: str) -> str:
    """Format a validated IP for curl's ``HOST:PORT:ADDRESS`` resolve syntax."""
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address):
        return f"[{address}]"
    return address


def _url_error(error: ValueError) -> PublicFetchError:
    """Map an address the policy or DNS refused to its precise failure."""
    if isinstance(error, _BlockedUrlError):
        code = "blocked_url"
    elif isinstance(error, _HostNotFoundError):
        code = "host_not_found"
    else:
        code = "invalid_url"
    return PublicFetchError(code, str(error))


def _shows_bot_check(url: str, result: PublicResponse) -> bool:
    """Whether an error answer is a browser check: a challenge address it redirected
    to (``/.enodia/challenge``), or the title of a self-solving check page."""
    final_path = urlparse(result.url).path.lower()
    if "challenge" in final_path and "challenge" not in urlparse(url).path.lower():
        return True
    title = _PAGE_TITLE.search(result.text[:8192])
    return title is not None and html.unescape(title[1]).strip().lower() in (
        SELF_SOLVING_CHECK_TITLES
    )


def _status_error(url: str, result: PublicResponse, what: str) -> PublicFetchError:
    """Describe an HTTP error status and whether another fetcher may still help."""
    host = urlparse(url).hostname or url
    status = result.status_code
    retryable = is_retryable_status(status, idempotent=True)
    attempts = result.attempts if retryable else None
    if status in {404, 410}:
        return PublicFetchError(
            f"{what}_not_found",
            f"HTTP {status}: there is no {what} at {url}. Check the address; the {what} may "
            "have moved or been removed.",
        )
    if status in {401, 403, 407}:
        code = "access_denied"
        message = (
            f"HTTP {status}: {host} refused access to {url}. The site blocks automated "
            "requests or requires a login, so repeating the request will not help. Try "
            "another source."
        )
    elif status == 429:
        code = "rate_limited"
        message = (
            f"HTTP 429: {host} is limiting requests. Wait before fetching from this site "
            "again, or try another source."
        )
    elif 500 <= status < 600:
        code = "server_error"
        message = (
            f"HTTP {status}: {host} failed to serve {url}. The site may be down; try again "
            "later or use another source."
        )
    elif status == 406:
        code = "request_rejected"
        message = (
            f"HTTP 406: {host} offers {url} in none of the formats requested, also not to "
            "an ordinary browser request. Try another source."
        )
    elif 400 <= status < 500 and _shows_bot_check(url, result):
        # The local browser can pass such a check (Enodia answers HTTP 400).
        code = "access_denied"
        message = (
            f"HTTP {status}: {host} answered the request for {url} with a bot check instead "
            f"of the {what}, so repeating the request will not help. Try another source."
        )
    elif 400 <= status < 500:
        code = "request_rejected"
        message = (
            f"HTTP {status}: {host} rejected the request for {url}. Check the address for "
            "mistakes; if it is right, try another source."
        )
    else:
        code = "unexpected_response"
        message = f"HTTP {status}: {host} answered without {what} content for {url}."
    return PublicFetchError(
        code,
        message,
        retryable=retryable,
        attempts_made=attempts,
        recoverable=not _global_rate_limit(result),
        retry_after=parse_retry_after(result.headers) if retryable else None,
    )


def _curl_detail(error: RequestException) -> str:
    """Return curl's own reason without its boilerplate and documentation link."""
    text = str(error)
    match = re.search(r"curl: \(\d+\)\s*(.+?)(?:\. See https?://\S+.*)?$", text, re.S)
    detail = (match[1] if match else text).strip().rstrip(".")
    return detail[:300]


# What curl's certificate reasons mean, in words a reader knows.
_CERTIFICATE_PROBLEMS = (
    ("expired", "it has expired"),
    ("not yet valid", "it is not valid yet"),
    ("certificate chain", "it was not issued by a trusted authority"),
    ("self signed", "it is self-signed"),
    ("self-signed", "it is self-signed"),
    ("issuer", "it was not issued by a trusted authority"),
    ("unknown ca", "it was not issued by a trusted authority"),
    ("subject name", "it was issued for another address"),
    ("does not match", "it was issued for another address"),
    ("revoked", "it has been revoked"),
)


def _tls_message(host: str, error: RequestException, detail: str) -> str:
    from curl_cffi.requests.exceptions import CertificateVerifyError

    if isinstance(error, CertificateVerifyError):
        lowered = detail.lower()
        problem = next((text for marker, text in _CERTIFICATE_PROBLEMS if marker in lowered), "")
        reason = f" ({problem})" if problem else ""
        return (
            f"The security certificate of {host} could not be verified{reason}, so the "
            "connection was not trusted. Repeating the request will not help; try another source."
        )
    return (
        f"{host} refused a secure connection with the settings current browsers use, as "
        "outdated or misconfigured servers do. Repeating the request will not help; try "
        "another source."
    )


def _transport_error(
    url: str, error: RequestException, attempts: int, hop: str | None = None
) -> PublicFetchError:
    """Describe a failure below HTTP: timeout, TLS or connection.

    ``hop`` is the address that failed when a redirect led away from ``url``.
    """
    from curl_cffi.requests.exceptions import DNSError, Timeout

    host = urlparse(hop or url).hostname or url
    if host != (urlparse(url).hostname or url):
        host = f"{host} (where {url} redirected)"
    detail = _curl_detail(error)
    if isinstance(error, Timeout):
        return PublicFetchError(
            "timeout",
            f"Fetching from {host} timed out. The site may be slow or unreachable; "
            "try again later or use another source.",
            retryable=True,
            attempts_made=attempts,
            recoverable=True,
        )
    if _is_tls_failure(error):
        return PublicFetchError("tls_error", _tls_message(host, error, detail), recoverable=True)
    if isinstance(error, DNSError):
        return PublicFetchError("host_not_found", _host_not_found(host), recoverable=True)
    return PublicFetchError(
        "connection_failed",
        f"The connection to {host} failed ({detail}). The site may be down or refusing "
        "connections; try again later or use another source.",
        retryable=True,
        attempts_made=attempts,
        recoverable=True,
    )


async def check_public_url(url: str) -> str:
    """Return the canonical host of a public HTTP(S) address, or raise ``PublicFetchError``."""
    try:
        host, _ip = await _validate_public_target(*_url_parts(url))
        return host
    except ValueError as error:
        raise _url_error(error) from error


async def resolve_public_address(host: str, port: int) -> str:
    """Return the public IP a connection to ``host:port`` must use.

    Raises ``PublicFetchError`` when the host is private, local or unresolvable.
    Callers connect to exactly the returned address, so a later DNS answer
    cannot swap in a private one.
    """
    try:
        return (await _validate_public_target("https", host, port))[1][0]
    except ValueError as error:
        raise _url_error(error) from error


async def fetch_public(
    url: str,
    *,
    accept: str,
    max_bytes: int = MAX_RESPONSE_BYTES,
    what: str = "page",
    hosts: HostSlots | None = None,
    permissions: PermissionBoundary | PermissionGrant,
    truncate: bool = False,
) -> PublicResponse:
    """GET a public address and return its successful response.

    A body longer than ``max_bytes`` fails, or with ``truncate`` is cut there.
    ``what`` names the expected content in failure messages ("page", "image").
    ``hosts`` bounds the requests running at once against each host, shared with
    the caller's other fetches; ``None`` leaves them unbounded. ``permissions``
    are checked for the address and every redirect hop before it is requested
    (``PermissionDenied``). Every other failure, including an HTTP error status,
    raises ``PublicFetchError``.
    """
    # A refused address opens no session.
    _check_hop(permissions, url, url)
    await _load_curl_cffi()
    from curl_cffi.requests.exceptions import RequestException

    try:
        async with _make_session(accept) as session:
            result = await _fetch_with_retry(
                session, url, {}, max_bytes, what, hosts, permissions=permissions, truncate=truncate
            )
        if result.status_code == 406:
            # The site serves none of the formats asked for; a browser's own
            # request is what sites answer most (a page refused text/markdown).
            async with _make_session(None) as session:
                result = await _fetch_with_retry(
                    session,
                    url,
                    {},
                    max_bytes,
                    what,
                    hosts,
                    permissions=permissions,
                    truncate=truncate,
                )
    except _RedirectTargetBlockedError as error:
        raise PublicFetchError("blocked_url", str(error)) from error
    except _RedirectTargetUnreachableError as error:
        raise PublicFetchError("redirect_unreachable", str(error), recoverable=True) from error
    except ValueError as error:
        raise _url_error(error) from error
    except _ResponseTooLargeError as error:
        raise PublicFetchError(
            "response_too_large", f"{url}: {error}. Try a smaller file or another source."
        ) from error
    except _RedirectLimitExceededError as error:
        raise PublicFetchError("redirect_loop", str(error), recoverable=True) from error
    except _TransportFailedError as failure:
        _LOGGER.warning("Public fetch failed for %s: %s", url, failure.error)
        raise _transport_error(url, failure.error, failure.attempts, failure.url) from failure
    except RequestException as error:
        _LOGGER.warning("Public fetch failed for %s: %s", url, error)
        raise _transport_error(url, error, 1) from error

    if not 200 <= result.status_code < 300:
        raise _status_error(url, result, what)
    return result


__all__ = [
    "HOST_CONCURRENCY",
    "MAX_RESPONSE_BYTES",
    "SELF_SOLVING_CHECK_TITLES",
    "HostSlots",
    "PublicFetchError",
    "PublicResponse",
    "check_public_url",
    "fetch_public",
    "hop_refusal",
    "resolve_public_address",
]
