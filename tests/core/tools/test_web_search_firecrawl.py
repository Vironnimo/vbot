"""web_search with Firecrawl: request body, native filters and its response shapes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import respx

from tests.core.tools.web_search_test_support import (
    API_KEY,
    FIRECRAWL_ENDPOINT,
    assert_failure_envelope,
    assert_success_envelope,
    request_json,
    result_urls,
    search,
)


async def _search_firecrawl(
    tmp_path: Path, arguments: dict[str, Any], body: dict[str, Any]
) -> tuple[dict[str, Any], respx.Route]:
    with respx.mock() as router:
        route = router.post(FIRECRAWL_ENDPOINT).respond(200, json=body)
        result = await search(tmp_path, arguments, provider="firecrawl")
    return result, route


@pytest.mark.asyncio
async def test_search_sends_the_key_and_lists_the_results(tmp_path: Path) -> None:
    result, route = await _search_firecrawl(
        tmp_path,
        {"query": "vbot", "count": 5},
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "vBot docs",
                        "url": "https://example.com/vbot",
                        "description": "vBot documentation",
                    },
                    {
                        "title": "vBot project",
                        "url": "https://example.com/project",
                        "description": "Project page",
                    },
                ]
            },
        },
    )

    assert assert_success_envelope(result) == {
        "content": (
            "1. vBot docs\nhttps://example.com/vbot\nvBot documentation\n\n"
            "2. vBot project\nhttps://example.com/project\nProject page"
        )
    }
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    body = request_json(request)
    assert body["query"] == "vbot"
    assert body["limit"] == 5
    assert body["sources"] == ["web"]
    assert "tbs" not in body
    assert "includeDomains" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recency", "tbs"),
    [("day", "qdr:d"), ("week", "qdr:w"), ("month", "qdr:m"), ("year", "qdr:y")],
)
async def test_recency_and_domains_are_native_filters(
    tmp_path: Path, recency: str, tbs: str
) -> None:
    result, route = await _search_firecrawl(
        tmp_path,
        {"query": "vbot", "domains": ["example.com"], "recency": recency},
        {
            "success": True,
            "data": {
                "web": [
                    {"title": "On-domain result", "url": "https://example.com/vbot"},
                    {"title": "Off-domain leak", "url": "https://other.test/vbot"},
                ]
            },
        },
    )

    data = assert_success_envelope(result)
    assert data["recency"] == recency
    assert result_urls(data) == ["https://example.com/vbot"]
    body = request_json(route.calls[0].request)
    assert body["tbs"] == tbs
    assert body["includeDomains"] == ["example.com"]
    assert "site:" not in body["query"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"success": False, "error": "concurrency limit reached"}, "concurrency limit reached"),
        ({"success": False, "message": "too many requests"}, "too many requests"),
    ],
)
async def test_an_unsuccessful_answer_is_a_failure_with_its_reason(
    tmp_path: Path, body: dict[str, Any], reason: str
) -> None:
    result, route = await _search_firecrawl(tmp_path, {"query": "vbot"}, body)

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == f"Firecrawl could not run the search: {reason}"
    assert error["retryable"] is False
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content"),
    [
        (
            {"success": True, "results": [{"title": "Docs", "url": "https://example.com/vbot"}]},
            "1. Docs\nhttps://example.com/vbot",
        ),
        (
            {"success": True, "data": [{"title": "Docs", "url": "https://example.com/vbot"}]},
            "1. Docs\nhttps://example.com/vbot",
        ),
        (
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "sourceURL": "https://example.com/vbot",
                            "snippet": "vBot documentation",
                            "publishedDate": "2026-08-20",
                            "metadata": {"title": "vBot docs"},
                        }
                    ]
                },
            },
            "1. vBot docs\nhttps://example.com/vbot\n2026-08-20 - vBot documentation",
        ),
    ],
    ids=["results-list", "data-list", "fallback-fields"],
)
async def test_other_response_shapes_are_read(
    tmp_path: Path, body: dict[str, Any], content: str
) -> None:
    result, _ = await _search_firecrawl(tmp_path, {"query": "vbot"}, body)

    assert assert_success_envelope(result)["content"] == content
