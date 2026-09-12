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
)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("recency", ["day", "month", "year"])
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
    assert data["provider"] == "searxng"
    assert "query" not in data
    assert "count_requested" not in data
    assert len(data["results"]) == 1
    assert data["recency"] == recency
    assert "filters" not in data
    assert data["warnings"] == ["recency enforcement depends on the configured SearXNG engines"]

    request = route.calls[0].request
    assert request.url.params["q"] == "vbot"
    assert request.url.params["format"] == "json"
    assert request.url.params["categories"] == "general"
    assert request.url.params["safesearch"] == "0"
    assert request.url.params["time_range"] == recency

    results = data["results"]
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["title"] == "vBot docs"
    assert results[0]["description"] == "vBot documentation"


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
    assert data["applied_domains"] == ["example.com"]
    assert len(data["results"]) == 1
    assert data["results"][0]["url"] == "https://docs.example.com/vbot"
    assert data["warnings"] == [
        "domain-filter completeness depends on the configured SearXNG engines; "
        "returned results are still restricted to applied_domains"
    ]
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

    assert_failure_envelope(result, "provider_request_failed")


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
    assert "page" not in data
    assert data["results"][0]["page_age"] == "2026-04-30T12:00:00+00:00"
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
    warnings = data.get("warnings", [])
    assert any("page size" in w.lower() for w in warnings), (
        f"expected a pagination warning, got {warnings}"
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
    assert "warnings" not in data
