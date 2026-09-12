"""Web search: retry behavior."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
import respx

import core.tools._web_search_transport as web_search_transport
from core.tools.web_search import (
    web_search_handler,
)
from core.utils.retry import MAX_RETRIES
from tests.core.tools.web_search_helpers import (
    _BRAVE_ENDPOINT,
    _DUCKDUCKGO_ENDPOINT,
    _EXA_ENDPOINT,
    _FIRECRAWL_ENDPOINT,
    _PERPLEXITY_ENDPOINT,
    _SEARXNG_ENDPOINT,
    _SERPER_ENDPOINT,
    _TAVILY_ENDPOINT,
    _fake_credential_resolver,
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
)


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_http_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(403, json={"error": {"detail": "forbidden"}})
    )

    with caplog.at_level(logging.WARNING, logger="vbot.tools.web_search"):
        result = await web_search_handler(
            make_context(workspace),
            {"query": "vbot"},
            _fake_credential_resolver,
        )

    assert_failure_envelope(result, "provider_request_failed")
    assert any(
        record.levelno == logging.WARNING
        and "Brave web search request failed" in record.getMessage()
        for record in caplog.records
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_network_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    sleep_attempts: list[int] = []

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del retry_after
        sleep_attempts.append(attempt)

    monkeypatch.setattr("core.tools._web_search_transport.sleep_for_retry", _fake_sleep)

    def _raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    route = respx.get(_BRAVE_ENDPOINT).mock(side_effect=_raise_connect_error)

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "provider_request_failed")
    assert len(route.calls) == 4
    assert sleep_attempts == [0, 1, 2]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 503])
async def test_web_search_handler_retries_transient_http_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    sleep_attempts: list[int] = []

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del retry_after
        sleep_attempts.append(attempt)

    monkeypatch.setattr("core.tools._web_search_transport.sleep_for_retry", _fake_sleep)

    route = respx.get(_BRAVE_ENDPOINT).mock(
        side_effect=[
            httpx.Response(status_code, json={"error": {"message": "temporary failure"}}),
            httpx.Response(200, json={"web": {"results": []}}),
        ]
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert len(data["results"]) == 0
    assert len(route.calls) == 2
    assert sleep_attempts == [0]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_brave_exhausted_status_signals_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr("core.tools._web_search_transport.sleep_for_retry", _fake_sleep)

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(503, json={"error": {"message": "busy"}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["retryable"] is True
    assert error["attempts_made"] == MAX_RETRIES + 1
    assert len(route.calls) == MAX_RETRIES + 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_brave_network_error_signals_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr("core.tools._web_search_transport.sleep_for_retry", _fake_sleep)

    def _raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    respx.get(_BRAVE_ENDPOINT).mock(side_effect=_raise_connect_error)

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["retryable"] is True
    assert error["attempts_made"] == MAX_RETRIES + 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_brave_non_retryable_status_signals_not_retryable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(403, json={"error": {"detail": "forbidden"}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["retryable"] is False
    assert "attempts_made" not in error


@pytest.mark.asyncio
async def test_web_search_validation_error_signals_not_retryable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "   "},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "validation_error")
    assert error["retryable"] is False


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_honors_retry_after_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    observed_hints: list[float | None] = []

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt
        observed_hints.append(retry_after)

    monkeypatch.setattr("core.tools._web_search_transport.sleep_for_retry", _fake_sleep)

    respx.get(_BRAVE_ENDPOINT).mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={"error": {"message": "rate limited"}},
            ),
            httpx.Response(200, json={"web": {"results": []}}),
        ]
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    assert_success_envelope(result)
    assert observed_hints == [7.0]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "method", "endpoint"),
    [
        ("brave", "GET", _BRAVE_ENDPOINT),
        ("searxng", "GET", _SEARXNG_ENDPOINT),
        ("duckduckgo", "GET", _DUCKDUCKGO_ENDPOINT),
        ("tavily", "POST", _TAVILY_ENDPOINT),
        ("exa", "POST", _EXA_ENDPOINT),
        ("serper", "POST", _SERPER_ENDPOINT),
        ("firecrawl", "POST", _FIRECRAWL_ENDPOINT),
        ("perplexity", "POST", _PERPLEXITY_ENDPOINT),
    ],
)
@pytest.mark.parametrize("status_code", [408, 500, 503])
async def test_web_search_preserves_provider_retry_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    method: str,
    endpoint: str,
    status_code: int,
) -> None:
    sleeps: list[tuple[int, float | None]] = []

    async def record_sleep(attempt: int, retry_after: float | None = None) -> None:
        sleeps.append((attempt, retry_after))

    monkeypatch.setattr(web_search_transport, "sleep_for_retry", record_sleep)
    route = respx.request(method, endpoint).respond(
        status_code,
        headers={"Retry-After": "7"},
        json={"message": "temporary failure"},
    )
    result = await web_search_handler(
        make_context(tmp_path),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": provider, "searxng": {"base_url": "http://localhost:8888"}},
    )

    retryable = (
        status_code == 503
        or (status_code == 500 and method == "GET")
        or (status_code == 408 and provider == "firecrawl")
    )
    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == f"HTTP {status_code}: temporary failure"
    assert error["retryable"] is retryable
    if retryable:
        assert error["attempts_made"] == MAX_RETRIES + 1
        assert route.call_count == MAX_RETRIES + 1
        assert sleeps == [(attempt, 7.0) for attempt in range(MAX_RETRIES)]
    else:
        assert "attempts_made" not in error
        assert route.call_count == 1
        assert sleeps == []
