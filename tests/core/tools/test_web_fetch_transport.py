"""web_fetch transport: public-address policy, bounded retries, redirects, precise
failures, and the pinned browser-impersonating request."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from curl_cffi import CurlOpt
from curl_cffi.requests.exceptions import CertificateVerifyError, ConnectTimeout, ReadTimeout
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

import core.tools._public_http as public_http
from core.tools._public_http import PublicResponse
from core.utils.retry import MAX_RETRIES
from tests.core.tools.web_fetch_test_support import (
    IPV6_ADDRESS,
    IPV6_HOST,
    StreamingResponse,
    StreamingSession,
    assert_failure_envelope,
    assert_success_envelope,
    fetch,
    install_http_get,
    make_result,
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
        ("https://localhost/admin", "private or local network address"),
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
async def test_redirect_to_a_private_address_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_url = "https://public.example/start"
    blocked_redirect = "http://127.0.0.1/admin"
    fetched: list[str] = []

    def responder(url: str) -> PublicResponse:
        fetched.append(url)
        return make_result(status_code=302, headers={"Location": blocked_redirect})

    install_http_get(monkeypatch, responder)

    result = await fetch(tmp_path, {"url": start_url})

    error = assert_failure_envelope(result, "blocked_url")
    assert error["message"].startswith(
        f"{start_url} redirected to an address that is not followed. 127.0.0.1 is a "
        "private or local network address; only public internet addresses can be "
        "fetched."
    )
    assert fetched == [start_url]


def _redirect_to_new_urls(request_url: str, calls: int) -> str:
    return f"https://example.com/loop-{calls}"


def _redirect_to_itself(request_url: str, calls: int) -> str:
    return request_url


def _redirect_back_and_forth(request_url: str, calls: int) -> str:
    return "https://example.com/second" if request_url.endswith("/first") else "/first"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "calls", "message"),
    [
        # Ever-new URLs exhaust the hop budget.
        (
            _redirect_to_new_urls,
            11,
            "https://example.com/first redirected more than 10 times without reaching a "
            "page. Try another source or a more direct address.",
        ),
        # A bot-deflection self-loop and an alternating pair fail on the first repeat.
        (
            _redirect_to_itself,
            1,
            "https://example.com/first redirects in a loop and never reaches a page; the "
            "site may require cookies or a browser. Try another source.",
        ),
        (
            _redirect_back_and_forth,
            2,
            "https://example.com/first redirects in a loop and never reaches a page; the "
            "site may require cookies or a browser. Try another source.",
        ),
    ],
    ids=["hop-limit", "self-loop", "alternating"],
)
async def test_redirect_loops_end_with_a_not_retryable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location, calls: int, message: str
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
    assert error["message"] == message
    assert error["retryable"] is False
    assert len(requested) == calls


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
            "HTTP 422: example.com rejected the request for https://example.com/page. "
            "The request was a plain GET without custom headers, cookies or a body.",
        ),
    ],
)
async def test_each_http_failure_is_named_with_its_next_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_sleeps: list[float | None],
    status: int,
    code: str,
    retryable: bool,
    message: str,
) -> None:
    requested: list[str] = []

    def responder(url: str) -> PublicResponse:
        requested.append(url)
        return make_result(status_code=status, text="no")

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
@pytest.mark.parametrize(
    ("error", "code", "attempts", "retryable", "message"),
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
            CertificateVerifyError(
                "Failed to perform, curl: (60) SSL certificate problem: certificate has "
                "expired. See https://curl.se/libcurl/c/libcurl-errors.html first for more "
                "details."
            ),
            "tls_error",
            1,
            False,
            "The secure connection to example.com failed (SSL certificate problem: "
            "certificate has expired). The site's certificate or TLS setup is broken, so "
            "repeating the request will not help.",
        ),
        (
            CurlConnectionError(
                "Failed to perform, curl: (7) Failed to connect to example.com port 443: "
                "Connection refused. See https://curl.se/libcurl/c/libcurl-errors.html first "
                "for more details."
            ),
            "connection_failed",
            MAX_RETRIES + 1,
            True,
            "The connection to example.com failed (Failed to connect to example.com port 443: "
            "Connection refused). The site may be down or refusing connections; try again "
            "later or use another source.",
        ),
    ],
    ids=["connect-timeout", "read-timeout", "broken-tls", "connection-refused"],
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
    message: str | None,
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
    if message is not None:
        assert failure["message"] == message
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
    ("url", "pin"),
    [
        ("https://example.com/page", "example.com:443:93.184.216.34"),
        ("http://93.184.216.34:8080/page", "93.184.216.34:8080:93.184.216.34"),
        (f"https://{IPV6_HOST}/page", f"{IPV6_HOST}:443:[{IPV6_ADDRESS}]"),
    ],
    ids=["resolved-host", "literal-ip", "ipv6"],
)
async def test_request_is_a_pinned_browser_like_get_with_a_streamed_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, pin: str
) -> None:
    session = StreamingSession(
        StreamingResponse(
            [b"hello ", b"world"], headers={"Content-Type": "text/plain; charset=utf-8"}
        )
    )
    session_factory = Mock(return_value=session)
    monkeypatch.setattr(public_http, "AsyncSession", session_factory)

    result = await fetch(tmp_path, {"url": url})

    assert assert_success_envelope(result)["content"] == "hello world"
    options = session_factory.call_args.kwargs
    assert options["impersonate"] == "chrome"
    assert "text/markdown" in options["headers"]["Accept"]
    # The connection targets exactly the address that cleared validation.
    assert session.curl_options[CurlOpt.RESOLVE] == [pin]
    assert session.calls == [
        ("GET", url, {"allow_redirects": False, "timeout": public_http._REQUEST_TIMEOUT})
    ]


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
    monkeypatch.setattr(public_http, "AsyncSession", lambda **_: StreamingSession(response))

    result = await fetch(tmp_path, {"url": "https://example.com/large"})

    error = assert_failure_envelope(result, "response_too_large")
    assert error["message"] == (
        "https://example.com/large: response exceeds the 50 MB download limit. Try a smaller "
        "file or another source."
    )
    assert error["retryable"] is False
