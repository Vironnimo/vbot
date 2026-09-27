"""web_search with a keyless SearXNG instance: request parameters, notes about engine
limits, and the configured address."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import respx

from tests.core.tools.web_search_test_support import (
    SEARXNG_ENDPOINT,
    assert_failure_envelope,
    assert_success_envelope,
    result_urls,
    search,
)


async def _search_searxng(
    tmp_path: Path, arguments: dict[str, Any], results: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, str]]:
    with respx.mock() as router:
        route = router.get(SEARXNG_ENDPOINT).respond(200, json={"results": results})
        result = await search(tmp_path, arguments, provider="searxng", credentials=lambda _: "")
    assert route.call_count == 1
    return assert_success_envelope(result), dict(route.calls[0].request.url.params)


@pytest.mark.asyncio
@pytest.mark.parametrize("recency", ["day", "week", "month", "year"])
async def test_search_needs_no_key_and_passes_recency_as_time_range(
    tmp_path: Path, recency: str
) -> None:
    data, params = await _search_searxng(
        tmp_path,
        {"query": "vbot", "count": 1, "recency": recency},
        [
            {"title": "vBot docs", "url": "https://example.com/vbot", "content": "vBot docs"},
            {"title": "vBot project", "url": "https://example.com/project", "content": "Page"},
        ],
    )

    assert data == {
        "recency": recency,
        "note": "Some SearXNG engines ignore the recency limit; check result dates.",
        "content": "1. vBot docs\nhttps://example.com/vbot\nvBot docs",
    }
    assert params == {
        "q": "vbot",
        "format": "json",
        "categories": "general",
        "safesearch": "0",
        "pageno": "1",
        "time_range": recency,
    }


@pytest.mark.asyncio
async def test_domains_are_enforced_before_the_count(tmp_path: Path) -> None:
    data, params = await _search_searxng(
        tmp_path,
        {"query": "vbot", "domains": ["example.com"], "count": 1},
        [
            {"title": "Off-domain first", "url": "https://other.test/vbot"},
            {"title": "Matching result", "url": "https://docs.example.com/vbot"},
        ],
    )

    assert data["domains"] == "example.com"
    assert result_urls(data) == ["https://docs.example.com/vbot"]
    assert data["note"] == (
        "Some SearXNG engines ignore site restrictions, so fewer results than requested may remain."
    )
    assert params["q"] == "vbot site:example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "results", "pageno", "data"),
    [
        (
            {"query": "vbot", "page": 2},
            [
                {
                    "title": "vBot docs",
                    "url": "https://example.com/vbot",
                    "content": "vBot documentation",
                    "publishedDate": "2026-04-30T12:00:00+00:00",
                }
            ],
            "2",
            {
                "note": "SearXNG uses its own page size, so results between pages may be skipped.",
                "content": (
                    "1. vBot docs\nhttps://example.com/vbot\n2026-04-30 - vBot documentation"
                ),
            },
        ),
        (
            {"query": "vbot"},
            [],
            "1",
            {"content": "No results found. Try other or fewer search words."},
        ),
    ],
    ids=["later-page", "first-page"],
)
async def test_later_pages_warn_that_results_may_be_skipped(
    tmp_path: Path,
    arguments: dict[str, Any],
    results: list[dict[str, Any]],
    pageno: str,
    data: dict[str, str],
) -> None:
    shown, params = await _search_searxng(tmp_path, arguments, results)

    assert shown == data
    assert params["pageno"] == pageno


@pytest.mark.asyncio
async def test_an_address_without_http_scheme_is_refused_without_searching(
    tmp_path: Path,
) -> None:
    with respx.mock() as router:
        result = await search(
            tmp_path,
            {"query": "vbot"},
            settings={"provider": "searxng", "searxng": {"base_url": "localhost:8888"}},
        )
        assert router.calls.call_count == 0

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == (
        "The SearXNG address in Settings (localhost:8888) is not an http or https URL. Tell "
        "the user to fix it in Settings under Web search."
    )
