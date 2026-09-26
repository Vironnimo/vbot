"""Web fetch: retry behavior."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from curl_cffi.requests.exceptions import CertificateVerifyError, ConnectTimeout, ReadTimeout
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

import core.tools._public_http as public_http
from core.tools._public_http import PublicResponse
from core.tools.tools import ToolRegistry
from core.tools.web_fetch import (
    register_web_fetch_tool,
)
from core.utils.retry import MAX_RETRIES
from tests.core.tools.web_fetch_helpers import (
    assert_failure_envelope,
    assert_success_envelope,
    install_http_get,
    make_context,
    make_result,
    web_fetch_arguments,
    web_fetch_handler,
)
from tests.core.tools.web_fetch_helpers import (
    stub_dns_resolution as stub_dns_resolution,
)
from tests.core.tools.web_fetch_helpers import (
    stub_http_session as stub_http_session,
)


@pytest.mark.asyncio
async def test_web_fetch_handler_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/not-found"

    install_http_get(
        monkeypatch, lambda _url: make_result(status_code=404, text="missing", url=url)
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "page_not_found")
    assert error["message"] == (
        "HTTP 404: there is no page at https://example.com/not-found. Check the address; "
        "the page may have moved or been removed."
    )


@pytest.mark.asyncio
async def test_web_fetch_handler_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/network-fail"

    def responder(_url: str) -> PublicResponse:
        raise CurlConnectionError(
            "Failed to perform, curl: (7) Failed to connect to example.com port 443: "
            "Connection refused. See https://curl.se/libcurl/c/libcurl-errors.html first "
            "for more details."
        )

    install_http_get(monkeypatch, responder)

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(public_http, "sleep_for_retry", no_retry_sleep)

    with caplog.at_level(logging.WARNING, logger="vbot.tools.public_http"):
        result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "connection_failed")
    assert error["message"] == (
        "The connection to example.com failed (Failed to connect to example.com port 443: "
        "Connection refused). The site may be down or refusing connections; try again "
        "later or use another source."
    )
    assert any(
        record.levelno == logging.WARNING and "Public fetch failed" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_web_fetch_handler_retries_retryable_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/retry"

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(public_http, "sleep_for_retry", no_retry_sleep)

    attempts = 0

    def responder(_url: str) -> PublicResponse:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return make_result(status_code=503, text="try later")
        return make_result(
            status_code=200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            text="retried success",
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    assert data["content"] == "retried success"
    assert attempts == 3


@pytest.mark.asyncio
async def test_web_fetch_handler_exhausted_retryable_status_signals_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/always-busy"

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(public_http, "sleep_for_retry", no_retry_sleep)

    calls = 0

    def responder(_url: str) -> PublicResponse:
        nonlocal calls
        calls += 1
        return make_result(status_code=503, text="busy")

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "server_error")
    assert error["retryable"] is True
    assert error["attempts_made"] == MAX_RETRIES + 1
    # All attempts were spent before the tool gave up.
    assert calls == MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_web_fetch_handler_honors_retry_after_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/rate-limited"

    observed_hints: list[float | None] = []

    async def recording_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt
        observed_hints.append(retry_after)

    monkeypatch.setattr(public_http, "sleep_for_retry", recording_sleep)

    attempts = 0

    def responder(_url: str) -> PublicResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return make_result(
                status_code=429,
                headers={"Retry-After": "7"},
                text="rate limited",
            )
        return make_result(
            status_code=200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            text="recovered",
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    assert data["content"] == "recovered"
    assert observed_hints == [7.0]


@pytest.mark.asyncio
async def test_web_fetch_handler_non_retryable_status_signals_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/not-found"

    install_http_get(monkeypatch, lambda _url: make_result(status_code=404, text="missing"))

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "page_not_found")
    assert error["retryable"] is False
    assert "attempts_made" not in error


@pytest.mark.asyncio
async def test_web_fetch_handler_transport_error_retries_before_signalling_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/network-fail"

    calls = 0

    def responder(_url: str) -> PublicResponse:
        nonlocal calls
        calls += 1
        raise CurlConnectionError("connection refused")

    install_http_get(monkeypatch, responder)

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(public_http, "sleep_for_retry", no_retry_sleep)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "connection_failed")
    assert error["retryable"] is True
    assert error["attempts_made"] == MAX_RETRIES + 1
    assert calls == MAX_RETRIES + 1


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
    ],
)
async def test_web_fetch_limits_retries_of_timeouts_and_broken_tls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(public_http, "sleep_for_retry", no_retry_sleep)

    result = await _registry().dispatch(make_context(tmp_path), {"url": "https://example.com/slow"})

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retryable", "message"),
    [
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
async def test_web_fetch_names_each_http_failure_and_its_next_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
    retryable: bool,
    message: str,
) -> None:
    install_http_get(monkeypatch, lambda _url: make_result(status_code=status, text="no"))

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(public_http, "sleep_for_retry", no_retry_sleep)

    result = await _registry().dispatch(make_context(tmp_path), {"url": "https://example.com/page"})

    error = assert_failure_envelope(result, code)
    assert error["message"].startswith(message)
    assert error["retryable"] is retryable
    assert ("attempts_made" in error) is retryable


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_web_fetch_tool(registry, attachment_store=None)
    return registry


@pytest.mark.asyncio
async def test_web_fetch_handler_recovers_from_transient_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/recovered"
    calls = 0

    def responder(_url: str) -> PublicResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CurlConnectionError("connection reset")
        return make_result(
            status_code=200,
            headers={"Content-Type": "text/plain"},
            text="recovered",
            url=url,
        )

    install_http_get(monkeypatch, responder)

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(public_http, "sleep_for_retry", no_retry_sleep)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    assert assert_success_envelope(result)["content"] == "recovered"
    assert calls == 2


@pytest.mark.asyncio
async def test_web_fetch_handler_redirect_limit_signals_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/loop"

    # A redirect chain of ever-new URLs exhausts the hop budget (a repeating
    # URL instead trips the faster cycle guard covered by its own tests).
    calls = 0

    def responder(request_url: str) -> PublicResponse:
        nonlocal calls
        calls += 1
        return make_result(
            status_code=302,
            headers={"Location": f"https://example.com/loop-{calls}"},
            url=request_url,
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "redirect_loop")
    assert error["retryable"] is False
    assert error["message"] == (
        "https://example.com/loop redirected more than 10 times without reaching a page. "
        "Try another source or a more direct address."
    )


@pytest.mark.asyncio
async def test_web_fetch_handler_redirect_cycle_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/self"
    calls = 0

    # A bot-deflection self-loop redirects to the identical URL forever.
    def responder(request_url: str) -> PublicResponse:
        nonlocal calls
        calls += 1
        return make_result(
            status_code=302,
            headers={"Location": request_url},
            url=request_url,
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "redirect_loop")
    assert error["retryable"] is False
    assert "redirects in a loop" in error["message"]
    assert calls == 1


@pytest.mark.asyncio
async def test_web_fetch_handler_alternating_redirect_cycle_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = "https://example.com/first"
    second = "https://example.com/second"
    calls = 0

    def responder(request_url: str) -> PublicResponse:
        nonlocal calls
        calls += 1
        other = second if request_url == first else first
        return make_result(
            status_code=302,
            headers={"Location": other},
            url=request_url,
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(first))

    error = assert_failure_envelope(result, "redirect_loop")
    assert error["retryable"] is False
    assert "redirects in a loop" in error["message"]
    assert calls == 2
