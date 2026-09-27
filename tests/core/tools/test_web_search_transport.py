"""web_search transport: provider refusals named with the next step, bounded retries
per provider and method, and bounded response bodies."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import core.tools._web_search_transport as web_search_transport
from core.utils.retry import MAX_RETRIES
from tests.core.tools.web_search_test_support import (
    BRAVE_ENDPOINT,
    DUCKDUCKGO_ENDPOINT,
    EXA_ENDPOINT,
    FIRECRAWL_ENDPOINT,
    PERPLEXITY_ENDPOINT,
    SEARXNG_ENDPOINT,
    SERPER_ENDPOINT,
    TAVILY_ENDPOINT,
    assert_failure_envelope,
    assert_success_envelope,
    search,
)
from tests.core.tools.web_search_test_support import retry_sleeps as retry_sleeps

_PROVIDERS = [
    ("brave", "Brave Search", "GET", BRAVE_ENDPOINT),
    ("searxng", "SearXNG", "GET", SEARXNG_ENDPOINT),
    ("duckduckgo", "DuckDuckGo", "GET", DUCKDUCKGO_ENDPOINT),
    ("tavily", "Tavily", "POST", TAVILY_ENDPOINT),
    ("exa", "Exa", "POST", EXA_ENDPOINT),
    ("serper", "Serper", "POST", SERPER_ENDPOINT),
    ("firecrawl", "Firecrawl", "POST", FIRECRAWL_ENDPOINT),
    ("perplexity", "Perplexity", "POST", PERPLEXITY_ENDPOINT),
]
_ENDPOINTS = {
    provider: (label, method, endpoint) for provider, label, method, endpoint in _PROVIDERS
}


def _key_message(label: str, status: int, answer: str, key: str) -> str:
    return (
        f"{label} rejected the API key (HTTP {status}: {answer}). Tell the user to check "
        f"{key} in the .env file of the vBot data directory."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "status", "response", "message"),
    [
        (
            "brave",
            403,
            {"json": {"error": {"detail": "forbidden"}}},
            _key_message("Brave Search", 403, "forbidden", "BRAVE_API_KEY"),
        ),
        (
            "brave",
            422,
            {
                "json": {
                    "type": "ErrorResponse",
                    "error": {
                        "code": "SUBSCRIPTION_TOKEN_INVALID",
                        "detail": "The provided subscription token is invalid.",
                    },
                }
            },
            _key_message(
                "Brave Search",
                422,
                "The provided subscription token is invalid.",
                "BRAVE_API_KEY",
            ),
        ),
        (
            "brave",
            422,
            {
                "json": {
                    "type": "ErrorResponse",
                    "error": {
                        "detail": "Unable to validate request parameter(s)",
                        "meta": {"errors": [{"loc": ["query", "q"], "msg": "String too long"}]},
                    },
                }
            },
            "Brave Search rejected the search (HTTP 422: Unable to validate request "
            "parameter(s); query.q: String too long).",
        ),
        (
            "brave",
            402,
            {"json": {"error": {"message": "Plan limit reached"}}},
            "Brave Search refused the search (HTTP 402: Plan limit reached); the account's "
            "plan or credits may be used up. Tell the user.",
        ),
        (
            "perplexity",
            401,
            {"json": {"error": "invalid key"}},
            _key_message("Perplexity", 401, "invalid key", "PERPLEXITY_API_KEY"),
        ),
        (
            "firecrawl",
            401,
            {"json": {"success": False, "error": "unauthorized"}},
            _key_message("Firecrawl", 401, "unauthorized", "FIRECRAWL_API_KEY"),
        ),
        (
            "serper",
            403,
            {"json": {"message": "invalid key"}},
            _key_message("Serper", 403, "invalid key", "SERPER_API_KEY"),
        ),
        (
            "tavily",
            401,
            {"json": {"detail": "Invalid API key"}},
            _key_message("Tavily", 401, "Invalid API key", "TAVILY_API_KEY"),
        ),
        (
            "exa",
            401,
            {"json": {"error": "invalid api key"}},
            _key_message("Exa", 401, "invalid api key", "EXA_API_KEY"),
        ),
        (
            "searxng",
            403,
            {
                "text": "<!doctype html>\n<html lang=en>\n<title>403 Forbidden</title>\n"
                "<h1>Forbidden</h1>"
            },
            "SearXNG refused the request (HTTP 403: 403 Forbidden). Tell the user to allow "
            "the json format under search.formats in the SearXNG instance's settings.",
        ),
    ],
    ids=[
        "brave-key",
        "brave-invalid-token",
        "brave-invalid-request",
        "brave-plan",
        "perplexity-key",
        "firecrawl-key",
        "serper-key",
        "tavily-key",
        "exa-key",
        "searxng-json-disabled",
    ],
)
async def test_provider_refusals_say_what_they_mean_and_are_not_repeated(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    provider: str,
    status: int,
    response: dict[str, Any],
    message: str,
) -> None:
    label, method, endpoint = _ENDPOINTS[provider]
    with respx.mock() as router:
        route = router.request(method, endpoint).respond(status, **response)
        with caplog.at_level(logging.WARNING, logger="vbot.tools.web_search"):
            result = await search(tmp_path, {"query": "vbot"}, provider=provider)

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == message
    assert error["retryable"] is False
    assert "attempts_made" not in error
    assert route.call_count == 1
    assert any(
        record.levelno == logging.WARNING
        and f"{label} web search request failed" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "message"),
    [
        (
            "brave",
            "Could not reach Brave Search (connection failed). The network or the service may "
            "be down; try again later.",
        ),
        (
            "searxng",
            "Could not reach SearXNG (connection failed). Tell the user the SearXNG instance "
            "at http://localhost:8888 is not reachable; they can start it or change its "
            "address in Settings under Web search.",
        ),
    ],
)
async def test_unreachable_providers_fail_after_every_attempt(
    tmp_path: Path,
    retry_sleeps: list[tuple[int, float | None]],
    provider: str,
    message: str,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    _, method, endpoint = _ENDPOINTS[provider]
    with respx.mock() as router:
        route = router.request(method, endpoint).mock(side_effect=refuse)
        result = await search(tmp_path, {"query": "vbot"}, provider=provider)

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == message
    assert error["retryable"] is True
    assert error["attempts_made"] == MAX_RETRIES + 1
    assert route.call_count == MAX_RETRIES + 1
    assert retry_sleeps == [(attempt, None) for attempt in range(MAX_RETRIES)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "failure", "success", "hint"),
    [
        (
            "brave",
            httpx.Response(429, json={"error": {"message": "temporary failure"}}),
            {"web": {"results": []}},
            None,
        ),
        (
            "brave",
            httpx.Response(503, json={"error": {"message": "temporary failure"}}),
            {"web": {"results": []}},
            None,
        ),
        (
            "brave",
            httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "rate limited"}),
            {"web": {"results": []}},
            7.0,
        ),
        # A POST is repeated only for statuses that show it did not run.
        (
            "tavily",
            httpx.Response(429, json={"detail": "rate limited"}),
            {"results": []},
            None,
        ),
        (
            "firecrawl",
            httpx.Response(408, json={"success": False, "error": "timed out"}),
            {"success": True, "data": {"web": []}},
            None,
        ),
    ],
    ids=["brave-429", "brave-503", "retry-after", "tavily-429", "firecrawl-408"],
)
async def test_transient_refusals_are_retried_until_results_arrive(
    tmp_path: Path,
    retry_sleeps: list[tuple[int, float | None]],
    provider: str,
    failure: httpx.Response,
    success: dict[str, Any],
    hint: float | None,
) -> None:
    _, method, endpoint = _ENDPOINTS[provider]
    with respx.mock() as router:
        route = router.request(method, endpoint).mock(
            side_effect=[failure, httpx.Response(200, json=success)]
        )
        result = await search(tmp_path, {"query": "vbot"}, provider=provider)

    assert assert_success_envelope(result) == {
        "content": "No results found. Try other or fewer search words."
    }
    assert route.call_count == 2
    assert retry_sleeps == [(0, hint)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "label", "method", "endpoint"), _PROVIDERS)
@pytest.mark.parametrize("status_code", [408, 500, 503])
async def test_each_provider_keeps_its_retry_profile(
    tmp_path: Path,
    retry_sleeps: list[tuple[int, float | None]],
    provider: str,
    label: str,
    method: str,
    endpoint: str,
    status_code: int,
) -> None:
    with respx.mock() as router:
        route = router.request(method, endpoint).respond(
            status_code,
            headers={"Retry-After": "7"},
            json={"message": "temporary failure"},
        )
        result = await search(tmp_path, {"query": "vbot"}, provider=provider)

    retryable = (
        status_code == 503
        or (status_code == 500 and method == "GET")
        or (status_code == 408 and provider == "firecrawl")
    )
    answer = f"HTTP {status_code}: temporary failure"
    if status_code == 408:
        expected = f"{label} rejected the search ({answer})."
    elif retryable:
        expected = f"{label} failed to answer after several attempts ({answer}). Try again later."
    else:
        expected = f"{label} failed to answer ({answer}). Try again later."
    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == expected
    assert error["retryable"] is retryable
    if retryable:
        assert error["attempts_made"] == MAX_RETRIES + 1
        assert route.call_count == MAX_RETRIES + 1
        assert retry_sleeps == [(attempt, 7.0) for attempt in range(MAX_RETRIES)]
    else:
        assert "attempts_made" not in error
        assert route.call_count == 1
        assert retry_sleeps == []


class _FailIfReadStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError("oversized declared response body must not be read")
        yield b""  # pragma: no cover


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "response"),
    [
        # A declared size over the limit is refused before the body is read.
        ("brave", {"headers": {"content-length": "6"}, "stream": _FailIfReadStream()}),
        # A body larger than it declared is cut off while streaming.
        ("searxng", {"headers": {"content-length": "1"}, "content": b"123456"}),
        ("tavily", {"headers": {"content-length": "1"}, "content": b"123456"}),
    ],
    ids=["declared", "streamed-get", "streamed-post"],
)
async def test_responses_over_the_size_limit_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, response: dict[str, Any]
) -> None:
    monkeypatch.setattr(web_search_transport, "_MAX_RESPONSE_BYTES", 5)
    _, method, endpoint = _ENDPOINTS[provider]
    with respx.mock() as router:
        router.request(method, endpoint).mock(return_value=httpx.Response(200, **response))
        result = await search(tmp_path, {"query": "vbot"}, provider=provider)

    error = assert_failure_envelope(result, "response_too_large")
    assert error["message"] == (
        "The search provider's response exceeds the 5 MB limit. Try again with a lower count."
    )
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_a_response_exactly_at_the_size_limit_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b'{"web":{"results":[]}}'
    monkeypatch.setattr(web_search_transport, "_MAX_RESPONSE_BYTES", len(body))
    with respx.mock() as router:
        router.get(BRAVE_ENDPOINT).respond(200, content=body)
        result = await search(tmp_path, {"query": "vbot"})

    assert assert_success_envelope(result) == {
        "content": "No results found. Try other or fewer search words."
    }
