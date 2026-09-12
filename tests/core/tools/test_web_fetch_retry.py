"""Web fetch: retry behavior."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

import core.tools.web_fetch as web_fetch_module
from core.tools.web_fetch import (
    _FetchResult,
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

    error = assert_failure_envelope(result, "request_error")
    assert "404" in error["message"]


@pytest.mark.asyncio
async def test_web_fetch_handler_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/network-fail"

    def responder(_url: str) -> _FetchResult:
        raise CurlConnectionError("connection refused")

    install_http_get(monkeypatch, responder)

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(web_fetch_module, "sleep_for_retry", no_retry_sleep)

    with caplog.at_level(logging.WARNING, logger="vbot.tools.web_fetch"):
        result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "request_error")
    assert "request failed" in error["message"].lower()
    assert any(
        record.levelno == logging.WARNING and "web_fetch request failed" in record.getMessage()
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

    monkeypatch.setattr(web_fetch_module, "sleep_for_retry", no_retry_sleep)

    attempts = 0

    def responder(_url: str) -> _FetchResult:
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

    monkeypatch.setattr(web_fetch_module, "sleep_for_retry", no_retry_sleep)

    calls = 0

    def responder(_url: str) -> _FetchResult:
        nonlocal calls
        calls += 1
        return make_result(status_code=503, text="busy")

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "request_error")
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

    monkeypatch.setattr(web_fetch_module, "sleep_for_retry", recording_sleep)

    attempts = 0

    def responder(_url: str) -> _FetchResult:
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

    error = assert_failure_envelope(result, "request_error")
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

    def responder(_url: str) -> _FetchResult:
        nonlocal calls
        calls += 1
        raise CurlConnectionError("connection refused")

    install_http_get(monkeypatch, responder)

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(web_fetch_module, "sleep_for_retry", no_retry_sleep)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is True
    assert error["attempts_made"] == MAX_RETRIES + 1
    assert calls == MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_web_fetch_handler_recovers_from_transient_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/recovered"
    calls = 0

    def responder(_url: str) -> _FetchResult:
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

    monkeypatch.setattr(web_fetch_module, "sleep_for_retry", no_retry_sleep)

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

    def responder(request_url: str) -> _FetchResult:
        nonlocal calls
        calls += 1
        return make_result(
            status_code=302,
            headers={"Location": f"https://example.com/loop-{calls}"},
            url=request_url,
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is False
    assert "too many redirects" in error["message"].lower()


@pytest.mark.asyncio
async def test_web_fetch_handler_redirect_cycle_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/self"
    calls = 0

    # A bot-deflection self-loop redirects to the identical URL forever.
    def responder(request_url: str) -> _FetchResult:
        nonlocal calls
        calls += 1
        return make_result(
            status_code=302,
            headers={"Location": request_url},
            url=request_url,
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is False
    assert "cycle" in error["message"].lower()
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

    def responder(request_url: str) -> _FetchResult:
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

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is False
    assert "cycle" in error["message"].lower()
    assert calls == 2
