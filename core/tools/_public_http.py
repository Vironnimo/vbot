"""Guarded HTTP GET for public internet addresses.

web_fetch reads pages with it and analyze_image downloads image URLs with it. The
guard is the same for every caller and is safety, not permission: only http(s)
URLs without credentials, only public addresses (the resolved address is pinned
for the connection, so DNS rebinding cannot swap in a private one), every
redirect hop revalidated, a bounded body and bounded retries. Failures carry
precise codes and wording that say what went wrong and what to try next.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlparse

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import (
    CertificateVerifyError,
    DNSError,
    RequestException,
    SSLError,
    Timeout,
)

from core.tools.tools import JsonObject, tool_failure
from core.utils.http_status import is_retryable_status, parse_retry_after
from core.utils.logging import get_logger
from core.utils.retry import MAX_RETRIES, sleep_for_retry

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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts_made = attempts_made
        self.recoverable = recoverable

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


class _ResponseTooLargeError(Exception):
    """Raised when a fetched response exceeds the bounded in-memory transfer limit."""


class _InvalidUrlError(ValueError):
    """The address is not a fetchable http(s) URL."""


class _BlockedUrlError(ValueError):
    """The address is valid but the public-URL policy refuses it."""


class _HostNotFoundError(ValueError):
    """The host name does not resolve."""


class _TransportFailedError(Exception):
    """A request failed below HTTP after the retries its kind allows."""

    def __init__(self, error: RequestException, attempts: int) -> None:
        self.error = error
        self.attempts = attempts
        super().__init__(str(error))


def _make_session(accept: str) -> AsyncSession:
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
    """
    return AsyncSession(impersonate=_IMPERSONATE_TARGET, headers={"Accept": accept})


def _article(what: str) -> str:
    return f"{'an' if what[:1] in 'aeiou' else 'a'} {what}"


def _size_label(max_bytes: int) -> str:
    megabytes, remainder = divmod(max_bytes, 1024 * 1024)
    return f"{megabytes} MB" if megabytes and not remainder else f"{max_bytes:,} bytes"


async def _http_get(session: AsyncSession, url: str, max_bytes: int) -> PublicResponse:
    """Perform a bounded GET with redirects disabled — the patchable network seam.

    Tests substitute this coroutine to feed canned responses without touching
    the network; production streams into one bounded byte buffer so a missing or
    dishonest ``Content-Length`` cannot make the process retain an unlimited body.
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
        if declared_size is not None and declared_size > max_bytes:
            raise _ResponseTooLargeError(too_large)

        body = bytearray()
        async for chunk in response.aiter_content():
            if len(body) + len(chunk) > max_bytes:
                raise _ResponseTooLargeError(too_large)
            body.extend(chunk)

        content = bytes(body)
        # curl_cffi decodes ``.text`` from this field using the response charset,
        # so keep its established decoding behavior after streaming the body.
        response.content = content
        return PublicResponse(
            status_code=response.status_code,
            headers=headers,
            text=response.text,
            url=str(response.url),
            content=content,
        )


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
        address.is_private
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


async def _validate_public_target(scheme: str, host: str | None, port: int) -> tuple[str, str]:
    """Validate a target against the SSRF blocklist and return ``(host, ip)``.

    Resolves the host, rejects any private/loopback/reserved address, and returns
    the normalized host together with the single public IP the connection must be
    pinned to. Returning the resolved address (rather than re-resolving at connect
    time) is what closes the DNS-rebinding window — the caller pins this exact IP
    via ``CurlOpt.RESOLVE`` (see ``_fetch_with_retry``).
    """
    if not scheme:
        raise _InvalidUrlError(
            "This is not a web address. Pass a full URL such as https://example.com/page."
        )
    if scheme not in {"http", "https"}:
        raise _InvalidUrlError(f"Only http and https URLs can be fetched, not {scheme}: URLs.")

    normalized_host = (host or "").rstrip(".").lower()
    if not normalized_host:
        raise _InvalidUrlError(
            "The URL has no host name. Pass a full URL such as https://example.com/page."
        )

    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise _BlockedUrlError(_private_address(normalized_host))

    literal_address = _parse_ip_literal(normalized_host)
    if literal_address is not None:
        if _is_blocked_ip(literal_address):
            raise _BlockedUrlError(_private_address(normalized_host))
        return normalized_host, str(literal_address)

    resolved_addresses = await _resolve_host_addresses(normalized_host, port)
    for resolved in resolved_addresses:
        if _is_blocked_ip(resolved):
            raise _BlockedUrlError(_private_address(normalized_host))
    return normalized_host, str(resolved_addresses[0])


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


def _retries_allowed(error: RequestException) -> int:
    """How often a transport failure is worth repeating.

    A broken certificate or TLS setup fails the same way every time. A timeout
    during connection or response transfer gets one more bounded attempt.
    """
    if isinstance(error, (SSLError, CertificateVerifyError, DNSError)):
        return 0
    if isinstance(error, Timeout):
        return 1
    return MAX_RETRIES


async def _request_with_retry(session: AsyncSession, url: str, max_bytes: int) -> PublicResponse:
    """Fetch a URL and retry transient transport/status failures with backoff."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            result = await _http_get(session, url, max_bytes)
        except RequestException as error:
            if attempt >= _retries_allowed(error):
                raise _TransportFailedError(error, attempt + 1) from error
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

        return result

    raise RuntimeError("unreachable retry loop state")


async def _fetch_with_retry(
    session: AsyncSession,
    url: str,
    resolve_map: dict[tuple[str, int], str],
    max_bytes: int = MAX_RESPONSE_BYTES,
    what: str = "page",
) -> PublicResponse:
    """Fetch a URL, validating each redirect hop against SSRF rules.

    Each hop's host is pinned to the IP that just cleared validation via curl's
    ``RESOLVE`` map, so the session connects to exactly the address that was
    checked while the hostname still drives Host header and TLS verification.
    ``resolve_map`` accumulates ``(host, port) -> ip`` across hops so an earlier
    hop's pin survives a same-host redirect.
    """
    current_url = url
    seen_urls: set[str] = set()

    for redirect_count in range(_MAX_REDIRECTS + 1):
        if current_url in seen_urls:
            # A redirect to an already-visited URL can never resolve (observed
            # as a bot-deflection self-loop); fail fast instead of burning
            # the whole hop budget one request at a time.
            raise _RedirectLimitExceededError(
                f"{url} redirects in a loop and never reaches {_article(what)}; the site "
                "may require cookies or a browser. Try another source."
            )
        seen_urls.add(current_url)
        try:
            scheme, host, port = _url_parts(current_url)
            normalized_host, pinned_ip = await _validate_public_target(scheme, host, port)
        except ValueError as error:
            if redirect_count > 0:
                raise _RedirectTargetBlockedError(
                    f"{url} redirected to an address that is not followed. {error}"
                ) from error
            raise
        resolve_map[(normalized_host, port)] = pinned_ip
        # curl's RESOLVE is an slist option and accepts a list; the type stub
        # narrows the dict value to str, so the assignment is annotated away.
        session.curl_options[CurlOpt.RESOLVE] = [  # type: ignore[assignment]
            f"{host}:{host_port}:{_curl_resolve_address(ip)}"
            for (host, host_port), ip in resolve_map.items()
        ]

        result = await _request_with_retry(session, current_url, max_bytes)
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


def _status_error(url: str, status: int, what: str) -> PublicFetchError:
    """Describe an HTTP error status and whether another fetcher may still help."""
    host = urlparse(url).hostname or url
    retryable = is_retryable_status(status, idempotent=True)
    attempts = MAX_RETRIES + 1 if retryable else None
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
    elif 400 <= status < 500:
        code = "request_rejected"
        message = (
            f"HTTP {status}: {host} rejected the request for {url}. The request was a plain "
            "GET without custom headers, cookies or a body."
        )
    else:
        code = "unexpected_response"
        message = f"HTTP {status}: {host} answered without {what} content for {url}."
    return PublicFetchError(
        code, message, retryable=retryable, attempts_made=attempts, recoverable=True
    )


def _curl_detail(error: RequestException) -> str:
    """Return curl's own reason without its boilerplate and documentation link."""
    text = str(error)
    match = re.search(r"curl: \(\d+\)\s*(.+?)(?:\. See https?://\S+.*)?$", text, re.S)
    detail = (match[1] if match else text).strip().rstrip(".")
    return detail[:300]


def _transport_error(url: str, error: RequestException, attempts: int) -> PublicFetchError:
    """Describe a failure below HTTP: timeout, TLS or connection."""
    host = urlparse(url).hostname or url
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
    if isinstance(error, (SSLError, CertificateVerifyError)):
        return PublicFetchError(
            "tls_error",
            f"The secure connection to {host} failed ({detail}). The site's certificate or "
            "TLS setup is broken, so repeating the request will not help.",
            recoverable=True,
        )
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


async def check_public_url(url: str) -> None:
    """Raise ``PublicFetchError`` unless ``url`` is a fetchable public http(s) address."""
    try:
        await _validate_public_target(*_url_parts(url))
    except ValueError as error:
        raise _url_error(error) from error


async def fetch_public(
    url: str,
    *,
    accept: str,
    max_bytes: int = MAX_RESPONSE_BYTES,
    what: str = "page",
) -> PublicResponse:
    """GET a public address and return its successful response.

    ``what`` names the expected content in failure messages ("page", "image").
    Every failure, including an HTTP error status, raises ``PublicFetchError``.
    """
    try:
        async with _make_session(accept) as session:
            result = await _fetch_with_retry(session, url, {}, max_bytes, what)
    except _RedirectTargetBlockedError as error:
        raise PublicFetchError("blocked_url", str(error)) from error
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
        raise _transport_error(url, failure.error, failure.attempts) from failure
    except RequestException as error:
        _LOGGER.warning("Public fetch failed for %s: %s", url, error)
        raise _transport_error(url, error, 1) from error

    if not 200 <= result.status_code < 300:
        raise _status_error(url, result.status_code, what)
    return result


__all__ = [
    "MAX_RESPONSE_BYTES",
    "PublicFetchError",
    "PublicResponse",
    "check_public_url",
    "fetch_public",
]
