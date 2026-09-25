"""Web search: searxng behavior."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from core.tools.web_search import (
    web_search_handler,
)
from tests.core.tools.web_search_helpers import (
    _SEARXNG_ENDPOINT,
    _fake_credential_resolver,
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
    result_urls,
)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("recency", ["day", "week", "month", "year"])
async def test_web_search_handler_searxng_maps_canonical_recency_without_api_key(
    tmp_path: Path,
    recency: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_SEARXNG_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "vBot docs",
                        "url": "https://example.com/vbot",
                        "content": "vBot documentation",
                    },
                    {
                        "title": "vBot project",
                        "url": "https://example.com/project",
                        "content": "Project page",
                    },
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 1, "recency": recency},
        lambda key: "",
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    data = assert_success_envelope(result)
    assert data == {
        "recency": recency,
        "note": "Some SearXNG engines ignore the recency limit; check result dates.",
        "content": "1. vBot docs\nhttps://example.com/vbot\nvBot documentation",
    }

    request = route.calls[0].request
    assert request.url.params["q"] == "vbot"
    assert request.url.params["format"] == "json"
    assert request.url.params["categories"] == "general"
    assert request.url.params["safesearch"] == "0"
    assert request.url.params["time_range"] == recency


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_enforces_domain_before_count(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_SEARXNG_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Off-domain first",
                        "url": "https://other.test/vbot",
                        "content": "Must be removed",
                    },
                    {
                        "title": "Matching result",
                        "url": "https://docs.example.com/vbot",
                        "content": "Must remain",
                    },
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": ["example.com"], "count": 1},
        lambda key: "",
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    data = assert_success_envelope(result)
    assert data["domains"] == "example.com"
    assert result_urls(data) == ["https://docs.example.com/vbot"]
    assert data["note"] == (
        "Some SearXNG engines ignore site restrictions, so fewer results than requested may remain."
    )
    assert route.calls[0].request.url.params["q"] == "vbot site:example.com"


@pytest.mark.asyncio
async def test_web_search_handler_searxng_rejects_invalid_base_url(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "searxng", "searxng": {"base_url": "localhost:8888"}},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == (
        "The SearXNG address in Settings (localhost:8888) is not an http or https URL. Tell "
        "the user to fix it in Settings under Web search."
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_unreachable_instance_tells_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr("core.tools._web_search_transport.sleep_for_retry", no_sleep)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed", request=request)

    respx.get(_SEARXNG_ENDPOINT).mock(side_effect=refuse)

    result = await web_search_handler(
        make_context(tmp_path),
        {"query": "vbot"},
        lambda key: "",
        lambda: {"provider": "searxng", "searxng": {"base_url": "http://localhost:8888"}},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == (
        "Could not reach SearXNG (All connection attempts failed). Tell the user the SearXNG "
        "instance at http://localhost:8888 is not reachable; they can start it or change its "
        "address in Settings under Web search."
    )
    assert error["retryable"] is True


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_html_refusal_is_summarized(tmp_path: Path) -> None:
    respx.get(_SEARXNG_ENDPOINT).respond(
        403,
        text="<!doctype html>\n<html lang=en>\n<title>403 Forbidden</title>\n<h1>Forbidden</h1>",
    )

    result = await web_search_handler(
        make_context(tmp_path),
        {"query": "vbot"},
        lambda key: "",
        lambda: {"provider": "searxng", "searxng": {"base_url": "http://localhost:8888"}},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == (
        "SearXNG refused the request (HTTP 403: 403 Forbidden). Tell the user to allow the "
        "json format under search.formats in the SearXNG instance's settings."
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_page_and_published_date(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_SEARXNG_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "vBot docs",
                        "url": "https://example.com/vbot",
                        "content": "vBot documentation",
                        "publishedDate": "2026-04-30T12:00:00+00:00",
                    }
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": 2},
        lambda key: "",
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    data = assert_success_envelope(result)
    assert data["content"] == (
        "1. vBot docs\nhttps://example.com/vbot\n2026-04-30 - vBot documentation"
    )
    assert route.calls[0].request.url.params["pageno"] == "2"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_page_warns_about_pagination_gap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_SEARXNG_ENDPOINT).mock(return_value=httpx.Response(200, json={"results": []}))

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": 2},
        lambda key: "",
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    data = assert_success_envelope(result)
    assert data["note"] == (
        "SearXNG uses its own page size, so results between pages may be skipped."
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_page1_has_no_pagination_warning(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_SEARXNG_ENDPOINT).mock(return_value=httpx.Response(200, json={"results": []}))

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        lambda key: "",
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    data = assert_success_envelope(result)
    assert "note" not in data
