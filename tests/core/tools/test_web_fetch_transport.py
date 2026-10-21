"""web_fetch transport: public-address policy, bounded retries, redirects, precise
failures, and the pinned browser-impersonating request."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlparse

import curl_cffi.requests
import pytest
from curl_cffi import CurlOpt
from curl_cffi.requests.exceptions import (
    CertificateVerifyError,
    ConnectTimeout,
    ReadTimeout,
    SSLError,
)
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

import core.tools._public_http as public_http
from core.tools._public_http import PublicResponse
from core.utils.retry import MAX_RETRIES
from tests.core.permissions.command_check_support import restricted
from tests.core.tools.web_fetch_test_support import (
    IPV6_ADDRESS,
    IPV6_HOST,
    StreamingResponse,
    StreamingSession,
    assert_failure_envelope,
    assert_success_envelope,
    fetch,
    install_http_get,
    make_context,
    make_result,
    web_fetch_registry,
)
from tests.core.tools.web_fetch_test_support import retry_sleeps as retry_sleeps
from tests.core.tools.web_fetch_test_support import stub_dns_resolution as stub_dns_resolution
from tests.core.tools.web_fetch_test_support import stub_http_session as stub_http_session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://example.com", "Only http and https URLs can be fetched, not ftp: URLs."),
        ("mailto:someone@example.com", "not mailto: URLs"),
        ("notes about the page", "This is not a web address."),
        ("https:///path-only", "The URL has no host name."),
    ],
)
async def test_addresses_that_are_not_web_urls_are_refused_without_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, message: str
) -> None:
    network = AsyncMock()
    monkeypatch.setattr(public_http, "_http_get", network)

    result = await fetch(tmp_path, {"url": url})

    error = assert_failure_envelope(result, "invalid_url")
    assert message in error["message"]
    assert error["retryable"] is False
    network.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://127.0.0.1/private", "private or local network address"),
        ("http://10.0.0.1/internal", "private or local network address"),
        ("http://100.64.0.1/internal", "private or local network address"),
        ("http://100.127.255.254/internal", "private or local network address"),
        ("http://[::ffff:100.64.0.1]/internal", "private or local network address"),
        ("https://localhost/admin", "private or local network address"),
        ("http://１２７.０.０.１/private", "private or local network address"),
        ("http://2130706433/private", "private or local network address"),
        ("http://0x7f000001/private", "private or local network address"),
        ("http://127.1/private", "private or local network address"),
        ("http://example.com@127.0.0.1/private", "user name or password"),
    ],
)
async def test_private_and_credential_addresses_are_blocked_without_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, message: str
) -> None:
    network = AsyncMock()
    monkeypatch.setattr(public_http, "_http_get", network)

    result = await fetch(tmp_path, {"url": url})

    error = assert_failure_envelope(result, "blocked_url")
    assert message in error["message"]
    assert error["retryable"] is False
    network.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("http://127.0.0.1/admin", "blocked_url"),
        ("http://100.64.0.1/admin", "blocked_url"),
        # A bot check redirects to a host that does not resolve (Swarm run 2026-10-13);
        # the Agent did not write that address, so it is not told to check it.
        ("https://validate.bot-check.invalid/?ssc=1", "redirect_unreachable"),
    ],
    ids=["private-address", "shared-address", "unresolvable-host"],
)
async def test_a_redirect_that_cannot_be_followed_says_where_it_led(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, code: str
) -> None:
    start_url = "https://public.example/start"
    fetched: list[str] = []
    resolve = public_http._resolve_host_addresses

    async def resolve_or_fail(host: str, port: int) -> list[public_http.IpAddress]:
        if host.endswith(".invalid"):
            raise public_http._HostNotFoundError(public_http._host_not_found(host))
        return await resolve(host, port)

    def responder(url: str) -> PublicResponse:
        fetched.append(url)
        return make_result(status_code=302, headers={"Location": location})

    monkeypatch.setattr(public_http, "_resolve_host_addresses", resolve_or_fail)
    install_http_get(monkeypatch, responder)

    result = await fetch(tmp_path, {"url": start_url})

    error = assert_failure_envelope(result, code)
    assert error["message"].startswith(f"{start_url} redirected to ")
    assert "typo" not in error["message"]
    assert fetched == [start_url]


def _redirect_to_new_urls(request_url: str, calls: int) -> str:
    return f"https://example.com/loop-{calls}"


def _redirect_to_itself(request_url: str, calls: int) -> str:
    return request_url


def _redirect_back_and_forth(request_url: str, calls: int) -> str:
    return "https://example.com/second" if request_url.endswith("/first") else "/first"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "calls"),
    [
        # Ever-new URLs exhaust the hop budget.
        (_redirect_to_new_urls, 11),
        # A bot-deflection self-loop and an alternating pair fail on the first repeat.
        (_redirect_to_itself, 1),
        (_redirect_back_and_forth, 2),
    ],
    ids=["hop-limit", "self-loop", "alternating"],
)
async def test_redirect_loops_end_with_a_not_retryable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location, calls: int
) -> None:
    requested: list[str] = []

    def responder(request_url: str) -> PublicResponse:
        requested.append(request_url)
        return make_result(
            status_code=302,
            headers={"Location": location(request_url, len(requested))},
            url=request_url,
        )

    install_http_get(monkeypatch, responder)

    result = await fetch(tmp_path, {"url": "https://example.com/first"})

    error = assert_failure_envelope(result, "redirect_loop")
    assert error["message"].startswith("https://example.com/first redirect")
    assert error["retryable"] is False
    assert len(requested) == calls


@pytest.mark.asyncio
async def test_a_redirect_back_after_a_login_service_set_cookies_reaches_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # nature.com and link.springer.com articles pass through their identity
    # service, which sets cookies and sends the request back (Swarm run 2026-10-13).
    article = "https://example.com/article"
    login = "https://idp.example.org/authorize"
    requested: list[str] = []

    async def http_get(session: StreamingSession, url: str, max_bytes: int) -> PublicResponse:
        del max_bytes
        requested.append(url)
        if url == login:
            session.cookies.set("session", "1", domain="example.com")
            return make_result(status_code=302, headers={"Location": article}, url=url)
        if session.cookies.get("session"):
            return make_result(headers={"Content-Type": "text/plain"}, text="Article", url=url)
        return make_result(status_code=303, headers={"Location": login}, url=url)

    monkeypatch.setattr(public_http, "_http_get", http_get)

    result = await fetch(tmp_path, {"url": article})

    assert assert_success_envelope(result)["content"] == "Article"
    assert requested == [article, login, article]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retryable", "message"),
    [
        (
            404,
            "page_not_found",
            False,
            "HTTP 404: there is no page at https://example.com/page. Check the address; the "
            "page may have moved or been removed.",
        ),
        (
            410,
            "page_not_found",
            False,
            "HTTP 410: there is no page at https://example.com/page. Check the address; the "
            "page may have moved or been removed.",
        ),
        (
            403,
            "access_denied",
            False,
            "HTTP 403: example.com refused access to https://example.com/page. The site "
            "blocks automated requests or requires a login, so repeating the request will "
            "not help. Try another source.",
        ),
        (
            401,
            "access_denied",
            False,
            "HTTP 401: example.com refused access to https://example.com/page.",
        ),
        (
            429,
            "rate_limited",
            True,
            "HTTP 429: example.com is limiting requests. Wait before fetching from this site "
            "again, or try another source.",
        ),
        (
            502,
            "server_error",
            True,
            "HTTP 502: example.com failed to serve https://example.com/page. The site may be "
            "down; try again later or use another source.",
        ),
        (
            422,
            "request_rejected",
            False,
            "HTTP 422: example.com rejected the request for https://example.com/page.",
        ),
        (
            (400, "<html><head><title>Enodia Verification</title></head></html>"),
            "access_denied",
            False,
            "HTTP 400: example.com answered the request for https://example.com/page with a "
            "bot check instead of the page, so repeating the request will not help. Try "
            "another source.",
        ),
    ],
)
async def test_each_http_failure_is_named_with_its_next_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_sleeps: list[float | None],
    status: int | tuple[int, str],
    code: str,
    retryable: bool,
    message: str,
) -> None:
    requested: list[str] = []
    status, page = status if isinstance(status, tuple) else (status, "no")

    def responder(url: str) -> PublicResponse:
        requested.append(url)
        return make_result(status_code=status, text=page)

    install_http_get(monkeypatch, responder)

    result = await fetch(tmp_path, {"url": "https://example.com/page"})

    error = assert_failure_envelope(result, code)
    assert error["message"].startswith(message)
    assert error["retryable"] is retryable
    # Retryable statuses spend every attempt before the Tool gives up.
    attempts = MAX_RETRIES + 1 if retryable else 1
    assert len(requested) == attempts
    if retryable:
        assert error["attempts_made"] == attempts
    else:
        assert "attempts_made" not in error


@pytest.mark.asyncio
@pytest.mark.parametrize("second_status", [200, 406], ids=["served", "refused-again"])
async def test_a_page_refusing_the_requested_formats_is_asked_once_more_like_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_status: int
) -> None:
    # A site answered HTTP 406 to text/markdown and served HTML (Swarm run 2026-10-13).
    sessions: list[dict[str, Any]] = []

    def make_session(**options: Any) -> StreamingSession:
        sessions.append(options)
        return StreamingSession()

    statuses = [406, second_status]
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", make_session)
    install_http_get(
        monkeypatch,
        lambda url: make_result(
            status_code=statuses.pop(0), headers={"Content-Type": "text/plain"}, text="Page"
        ),
    )

    result = await fetch(tmp_path, {"url": "https://example.com/page"})

    assert statuses == []
    assert "text/markdown" in sessions[0]["headers"]["Accept"]
    # The second request carries the impersonated browser's own Accept header.
    assert sessions[1]["headers"] == {}
    if second_status == 200:
        assert assert_success_envelope(result)["content"] == "Page"
    else:
        assert assert_failure_envelope(result, "request_rejected")["retryable"] is False


def _curl_error(kind: type[Exception], code: int, reason: str) -> Exception:
    return kind(
        f"Failed to perform, curl: ({code}) {reason}. See "
        "https://curl.se/libcurl/c/libcurl-errors.html first for more details."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code", "attempts", "retryable", "reason"),
    [
        (
            ConnectTimeout("Connection timed out after 5001 milliseconds"),
            "timeout",
            2,
            True,
            None,
        ),
        (
            ReadTimeout("Operation timed out after 30001 milliseconds"),
            "timeout",
            2,
            True,
            None,
        ),
        (
            _curl_error(
                CertificateVerifyError, 60, "SSL certificate problem: certificate has expired"
            ),
            "tls_error",
            1,
            False,
            "expired",
        ),
        (
            _curl_error(
                SSLError,
                35,
                "TLS connect error: error:10000410:SSL routines:OPENSSL_internal:"
                "SSLV3_ALERT_HANDSHAKE_FAILURE",
            ),
            "tls_error",
            1,
            False,
            "example.com",
        ),
        # A peer that cuts the TLS handshake off is a connection failure, not
        # broken TLS (Swarm run 2026-10-12: the same page loaded 17 s later).
        (
            _curl_error(SSLError, 35, "Recv failure: Connection was reset"),
            "connection_failed",
            MAX_RETRIES + 1,
            True,
            "Recv failure: Connection was reset",
        ),
        (
            _curl_error(
                CurlConnectionError,
                7,
                "Failed to connect to example.com port 443: Connection refused",
            ),
            "connection_failed",
            MAX_RETRIES + 1,
            True,
            "Failed to connect to example.com port 443: Connection refused",
        ),
    ],
    ids=[
        "connect-timeout",
        "read-timeout",
        "broken-certificate",
        "refused-handshake",
        "reset-handshake",
        "connection-refused",
    ],
)
async def test_transport_failures_are_retried_only_as_far_as_useful(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    retry_sleeps: list[float | None],
    error: Exception,
    code: str,
    attempts: int,
    retryable: bool,
    reason: str | None,
) -> None:
    calls = 0

    def responder(_url: str) -> PublicResponse:
        nonlocal calls
        calls += 1
        raise error

    install_http_get(monkeypatch, responder)

    with caplog.at_level(logging.WARNING, logger="vbot.tools.public_http"):
        result = await fetch(tmp_path, {"url": "https://example.com/slow"})

    failure = assert_failure_envelope(result, code)
    # No curl boilerplate or OpenSSL codes; a TLS failure says what failed in plain words.
    assert not re.search(r"curl|SSL|_ALERT_", failure["message"])
    if reason is not None:
        assert reason in failure["message"]
    else:
        # A connect timeout can end much earlier than the response limit.
        # Report the timeout without claiming a duration we did not measure.
        assert not re.search(r"\b\d+\s+(?:milli)?seconds?\b", failure["message"])
    assert failure["retryable"] is retryable
    assert calls == attempts
    if retryable:
        assert failure["attempts_made"] == attempts
    else:
        assert "attempts_made" not in failure
    assert any(
        record.levelno == logging.WARNING and "Public fetch failed" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failures", "hints"),
    [
        ([make_result(status_code=503, text="try later")] * 2, [None, None]),
        ([CurlConnectionError("connection reset")], [None]),
        ([make_result(status_code=429, headers={"Retry-After": "7"})], [7.0]),
    ],
    ids=["server-busy", "connection-reset", "retry-after"],
)
async def test_transient_failures_recover_on_a_later_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_sleeps: list[float | None],
    failures: list[PublicResponse | Exception],
    hints: list[float | None],
) -> None:
    outcomes = list(failures)
    calls = 0

    def responder(_url: str) -> PublicResponse:
        nonlocal calls
        calls += 1
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return make_result(headers={"Content-Type": "text/plain"}, text="recovered")

    install_http_get(monkeypatch, responder)

    result = await fetch(tmp_path, {"url": "https://example.com/flaky"})

    assert assert_success_envelope(result)["content"] == "recovered"
    assert calls == len(failures) + 1
    assert retry_sleeps == hints


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "request_url", "pins"),
    [
        ("https://example.com/page", "https://example.com/page", ["example.com:443:93.184.216.34"]),
        (
            "https://ExAmPle.com./page?q=1",
            "https://example.com/page?q=1",
            ["example.com:443:93.184.216.34"],
        ),
        (
            "https://bücher.example:8443/page",
            "https://xn--bcher-kva.example:8443/page",
            ["xn--bcher-kva.example:8443:93.184.216.34"],
        ),
        ("http://93.184.216.34:8080/page", "http://93.184.216.34:8080/page", []),
        (
            f"https://{IPV6_HOST}/page",
            f"https://{IPV6_HOST}/page",
            [f"{IPV6_HOST}:443:[{IPV6_ADDRESS}]"],
        ),
        (f"https://[{IPV6_ADDRESS}]:8443/page", f"https://[{IPV6_ADDRESS}]:8443/page", []),
    ],
    ids=["resolved-host", "trailing-dot", "idn", "literal-ip", "ipv6-dns", "ipv6-literal"],
)
async def test_request_is_a_pinned_browser_like_get_with_a_streamed_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, request_url: str, pins: list[str]
) -> None:
    session = StreamingSession(
        StreamingResponse(
            [b"hello ", b"world"], headers={"Content-Type": "text/plain; charset=utf-8"}
        )
    )
    session_factory = Mock(return_value=session)
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", session_factory)

    result = await fetch(tmp_path, {"url": url})

    assert assert_success_envelope(result)["content"] == "hello world"
    options = session_factory.call_args.kwargs
    assert options["impersonate"] == "chrome"
    assert "text/markdown" in options["headers"]["Accept"]
    # The connection targets exactly the address that cleared validation.
    assert session.curl_options[CurlOpt.RESOLVE] == pins
    assert session.calls == [
        ("GET", request_url, {"allow_redirects": False, "timeout": public_http._REQUEST_TIMEOUT})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "body", "text"),
    [
        ("text/plain; charset=iso-8859-1", b"Gr\xfc\xdfe \x80 5", "Grüße € 5"),
        ("text/plain; charset=iso-8859-1", b"\xef\xbb\xbfGr\xc3\xbc\xc3\x9fe", "Grüße"),
        (
            "text/html",
            b'<html><head><meta http-equiv="Content-Type" content="text/html; '
            b'charset=iso-8859-1" /><title>Gru\xdf</title></head>'
            b"<body><main><p>Gr\xfc\xdfe aus K\xf6ln</p></main></body></html>",
            "Grüße aus Köln",
        ),
        ("text/html", b"<p>Gr\xc3\xbc\xc3\x9fe aus K\xc3\xb6ln</p>", "Grüße aus Köln"),
        ("text/plain", b"Gr\xfc\xdfe \x93aus\x94 K\xf6ln", "Grüße \u201caus\u201d Köln"),
    ],
    ids=["header-charset", "byte-order-mark", "meta-charset", "undeclared-utf-8", "undeclared"],
)
async def test_bodies_are_decoded_as_a_browser_decodes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content_type: str, body: bytes, text: str
) -> None:
    response = StreamingResponse([body], headers={"Content-Type": content_type})
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", lambda **_: StreamingSession(response))

    result = await fetch(tmp_path, {"url": "https://example.com/stream"})

    content = assert_success_envelope(result)["content"]
    assert text in content and "\ufffd" not in content


@pytest.mark.asyncio
async def test_redirect_hosts_use_the_same_canonical_url_and_dns_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, list[str]]] = []
    redirects = ["https://bücher.example.:8443/page", f"https://[{IPV6_ADDRESS}]/page"]

    async def get(session: Any, url: str, max_bytes: int) -> PublicResponse:
        calls.append((url, list(session.curl_options[CurlOpt.RESOLVE])))
        if len(calls) <= len(redirects):
            return make_result(status_code=302, headers={"Location": redirects[len(calls) - 1]})
        return make_result(text="public content", headers={"Content-Type": "text/plain"}, url=url)

    monkeypatch.setattr(public_http, "_http_get", get)
    result = await fetch(tmp_path, {"url": "https://example.com./start"})
    assert assert_success_envelope(result)["content"] == "public content"
    first = "example.com:443:93.184.216.34"
    second = "xn--bcher-kva.example:8443:93.184.216.34"
    assert calls == [
        ("https://example.com/start", [first]),
        ("https://xn--bcher-kva.example:8443/page", [first, second]),
        (f"https://[{IPV6_ADDRESS}]/page", [first, second]),
    ]


@pytest.mark.asyncio
async def test_calls_share_a_bounded_number_of_requests_per_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active: Counter[str] = Counter()
    peak: Counter[str] = Counter()
    release = asyncio.Event()

    async def get(session: Any, url: str, max_bytes: int) -> PublicResponse:
        host = urlparse(url).hostname or ""
        active[host] += 1
        peak[host] = max(peak[host], active[host])
        try:
            if host == "example.com":
                await release.wait()
            return make_result(text="Page", headers={"Content-Type": "text/plain"}, url=url)
        finally:
            active[host] -= 1

    async def settle() -> None:
        for _ in range(200):
            await asyncio.sleep(0)

    monkeypatch.setattr(public_http, "_http_get", get)
    tool, context = web_fetch_registry(), make_context(tmp_path)
    pages = [f"https://example.com/{index}" for index in range(6)]
    calls = asyncio.gather(
        tool.dispatch(context, {"urls": pages[:3]}),
        tool.dispatch(context, {"urls": pages[3:]}),
        tool.dispatch(context, {"url": "https://other.example/"}),
    )
    await settle()

    # Two calls hold the host's slots together; another host is not kept waiting.
    assert active["example.com"] == public_http.HOST_CONCURRENCY
    assert peak["other.example"] == 1 and active["other.example"] == 0
    release.set()
    results = await asyncio.wait_for(calls, timeout=5)
    assert all(result["ok"] for result in results)
    assert peak["example.com"] == public_http.HOST_CONCURRENCY


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["declared", "streamed"])
async def test_responses_over_the_download_limit_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delivery: str
) -> None:
    limit = public_http.MAX_RESPONSE_BYTES
    response = (
        StreamingResponse([], headers={"Content-Length": str(limit + 1)})
        if delivery == "declared"
        # Neither chunk alone exceeds the limit; together they do.
        else StreamingResponse([b"x", bytes(limit)])
    )
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", lambda **_: StreamingSession(response))

    result = await fetch(tmp_path, {"url": "https://example.com/large"})

    error = assert_failure_envelope(result, "response_too_large")
    assert error["message"] == (
        "https://example.com/large: response exceeds the 50 MB download limit. Try a smaller "
        "file or another source."
    )
    assert error["retryable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "requested", "refused"),
    [
        ("https://denied.example/page", [], "denied.example,"),
        (
            "https://public.example/start",
            ["https://public.example/start"],
            "https://public.example/start, which leads to denied.example,",
        ),
    ],
    ids=["address", "redirect-hop"],
)
async def test_the_permission_rules_refuse_an_address_or_redirect_hop_before_it_is_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    requested: list[str],
    refused: str,
) -> None:
    fetched: list[str] = []

    def responder(request_url: str) -> PublicResponse:
        fetched.append(request_url)
        return make_result(
            status_code=302, headers={"Location": "https://denied.example/page"}, url=request_url
        )

    install_http_get(monkeypatch, responder)
    boundary = restricted(tmp_path, deny=["Web(denied.example)"])

    result = await fetch(tmp_path, {"url": url}, permissions=boundary)

    error = assert_failure_envelope(result, "permission_denied")
    assert error["message"].startswith(
        f"The permission rules refuse fetching {refused} because the rule "
        "Web(denied.example) denies it."
    )
    assert error["retryable"] is False
    assert fetched == requested
