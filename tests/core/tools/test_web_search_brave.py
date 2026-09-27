"""web_search with Brave Search, the default provider: request parameters, site
filters enforced on the results, and the result text."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tests.core.tools.web_search_test_support import (
    API_KEY,
    BRAVE_ENDPOINT,
    assert_success_envelope,
    result_urls,
    search,
)


async def _search_brave(
    tmp_path: Path, arguments: dict[str, Any], results: Any, **options: Any
) -> tuple[dict[str, Any], httpx.Request]:
    """Answer one Brave request with ``results`` (the whole body when a dict)."""
    body = results if isinstance(results, dict) else {"web": {"results": results}}
    with respx.mock() as router:
        route = router.get(BRAVE_ENDPOINT).respond(200, json=body)
        result = await search(tmp_path, arguments, **options)
    assert route.call_count == 1
    return assert_success_envelope(result), route.calls[0].request


@pytest.mark.asyncio
async def test_search_sends_the_key_and_query_and_lists_the_results(tmp_path: Path) -> None:
    data, request = await _search_brave(
        tmp_path,
        {"query": "vbot", "count": 5},
        [
            {
                "title": "vBot docs",
                "url": "https://example.com/vbot",
                "description": "vBot documentation",
            }
        ],
    )

    assert request.headers["X-Subscription-Token"] == API_KEY
    assert request.headers["Accept"] == "application/json"
    assert request.url.params["q"] == "vbot"
    assert request.url.params["count"] == "5"
    assert data == {"content": "1. vBot docs\nhttps://example.com/vbot\nvBot documentation"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "settings", "sent", "unsent"),
    [
        ({"query": "vbot"}, None, {"count": "12", "text_decorations": "false"}, ["offset"]),
        ({"query": "vbot"}, {"provider": "brave", "default_count": 7}, {"count": "7"}, []),
        ({"query": "vbot", "page": 3}, None, {"offset": "2"}, []),
    ],
    ids=["defaults", "configured-count", "page"],
)
async def test_count_and_page_follow_the_call_and_settings(
    tmp_path: Path,
    arguments: dict[str, Any],
    settings: dict[str, Any] | None,
    sent: dict[str, str],
    unsent: list[str],
) -> None:
    _, request = await _search_brave(tmp_path, arguments, [], settings=settings)

    assert {key: request.url.params[key] for key in sent} == sent
    assert all(key not in request.url.params for key in unsent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recency", "freshness"), [("day", "pd"), ("week", "pw"), ("month", "pm"), ("year", "py")]
)
async def test_recency_maps_to_brave_freshness(
    tmp_path: Path, recency: str, freshness: str
) -> None:
    data, request = await _search_brave(tmp_path, {"query": "vbot", "recency": recency}, [])

    tip = "no recency limit" if recency == "year" else "a longer recency window"
    assert data == {
        "recency": recency,
        "content": f"No results found. Try other or fewer search words, or {tip}.",
    }
    assert request.url.params["freshness"] == freshness


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domains", "results", "label", "urls", "query"),
    [
        (
            ["Example.COM.", "docs.example.com", "example.com"],
            [
                "https://example.com/docs",
                "https://docs.example.com/vbot",
                "https://example.com.evil.test/vbot",
                "https://other.test/?next=https://example.com",
            ],
            "example.com, docs.example.com",
            ["https://example.com/docs", "https://docs.example.com/vbot"],
            "vbot site:example.com OR site:docs.example.com",
        ),
        (
            ["www.example.com"],
            ["https://example.com/vbot", "https://www.example.com/vbot"],
            "www.example.com",
            ["https://www.example.com/vbot"],
            "vbot site:www.example.com",
        ),
        (
            ["FAẞ.example."],
            ["https://faß.example/vbot"],
            "xn--fa-hia.example",
            ["https://faß.example/vbot"],
            "vbot site:xn--fa-hia.example",
        ),
    ],
    ids=["site-and-subdomains", "specific-subdomain", "internationalized"],
)
async def test_domains_are_sent_as_site_operators_and_enforced_on_results(
    tmp_path: Path,
    domains: list[str],
    results: list[str],
    label: str,
    urls: list[str],
    query: str,
) -> None:
    data, request = await _search_brave(
        tmp_path,
        {"query": "vbot", "domains": domains},
        [{"title": f"Result {index}", "url": url} for index, url in enumerate(results)],
    )

    assert data["domains"] == label
    assert result_urls(data) == urls
    assert request.url.params["q"] == query


@pytest.mark.asyncio
async def test_query_operators_pass_through_unchanged(tmp_path: Path) -> None:
    query = 'vbot "agent loop" -draft filetype:pdf site:example.com/docs'

    data, request = await _search_brave(tmp_path, {"query": query}, [])

    assert "domains" not in data
    assert request.url.params["q"] == query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "content"),
    [
        (
            [
                {
                    "title": "<strong>vBot</strong> docs",
                    "url": "https://example.com/vbot",
                    "description": "The <strong>vBot</strong> docs &amp; guides",
                    "page_age": "2026-05-01T00:00:00",
                },
                {
                    "title": "vBot news",
                    "url": "https://example.com/news",
                    "description": "No date on this one",
                },
            ],
            "1. vBot docs\nhttps://example.com/vbot\n2026-05-01 - The vBot docs & guides\n\n"
            "2. vBot news\nhttps://example.com/news\nNo date on this one",
        ),
        (
            [
                {
                    "title": "A <b title='a > b'>title</b>",
                    "url": "https://example.com",
                    "description": "x < y and z > w &amp; more",
                }
            ],
            "1. A title\nhttps://example.com\nx < y and z > w & more",
        ),
    ],
    ids=["markup-and-page-age", "plain-comparisons"],
)
async def test_result_markup_is_stripped_and_plain_text_kept(
    tmp_path: Path, results: list[dict[str, Any]], content: str
) -> None:
    data, _ = await _search_brave(tmp_path, {"query": "vbot"}, results)

    assert data["content"] == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "data"),
    [
        (
            {"query": "vbot", "page": 2},
            {
                "more": "More results are available with page 3.",
                "content": "1. Example result\nhttps://example.com/vbot",
            },
        ),
        # Brave counts unfiltered results, so a site filter hides the offer.
        (
            {"query": "vbot", "domains": ["example.com"]},
            {"domains": "example.com", "content": "1. Example result\nhttps://example.com/vbot"},
        ),
    ],
    ids=["unfiltered", "site-filtered"],
)
async def test_more_results_name_the_next_page_only_without_site_filters(
    tmp_path: Path, arguments: dict[str, Any], data: dict[str, str]
) -> None:
    shown, _ = await _search_brave(
        tmp_path,
        arguments,
        {
            "web": {"results": [{"title": "Example result", "url": "https://example.com/vbot"}]},
            "query": {"more_results_available": True},
        },
    )

    assert shown == data
