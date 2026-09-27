"""web_search with Tavily and Exa: request bodies and native site and recency filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx

from tests.core.tools.web_search_test_support import (
    API_KEY,
    EXA_ENDPOINT,
    TAVILY_ENDPOINT,
    assert_success_envelope,
    make_context,
    request_json,
    result_urls,
    search,
    web_search_registry,
)


async def _search(
    tmp_path: Path, provider: str, arguments: dict[str, Any], results: list[dict[str, Any]]
) -> tuple[dict[str, Any], Any]:
    endpoint = TAVILY_ENDPOINT if provider == "tavily" else EXA_ENDPOINT
    with respx.mock() as router:
        route = router.post(endpoint).respond(200, json={"results": results})
        result = await search(tmp_path, arguments, provider=provider)
    assert route.call_count == 1
    return assert_success_envelope(result), route.calls[0].request


@pytest.mark.asyncio
async def test_tavily_gets_a_basic_search_and_lists_the_results(tmp_path: Path) -> None:
    data, request = await _search(
        tmp_path,
        "tavily",
        {"query": "vbot", "count": 5},
        [
            {
                "title": "vBot docs",
                "url": "https://example.com/vbot",
                "content": "vBot documentation",
                "published_date": "2026-08-20",
            },
            {"title": "vBot project", "url": "https://example.com/project", "content": "Page"},
            {"title": "", "url": "", "content": ""},
        ],
    )

    assert data == {
        "content": (
            "1. vBot docs\nhttps://example.com/vbot\n2026-08-20 - vBot documentation\n\n"
            "2. vBot project\nhttps://example.com/project\nPage"
        )
    }
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    body = request_json(request)
    assert body["query"] == "vbot"
    assert body["max_results"] == 5
    assert body["search_depth"] == "basic"
    assert body["include_answer"] is False
    assert "time_range" not in body
    assert "include_domains" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("recency", ["day", "week", "month", "year"])
async def test_tavily_filters_recency_and_domains_natively(tmp_path: Path, recency: str) -> None:
    data, request = await _search(
        tmp_path,
        "tavily",
        {"query": "vbot", "domains": ["example.com"], "recency": recency},
        [
            {"title": "On-domain result", "url": "https://example.com/vbot"},
            {"title": "Off-domain leak", "url": "https://other.test/vbot"},
        ],
    )

    assert data["recency"] == recency
    assert result_urls(data) == ["https://example.com/vbot"]
    body = request_json(request)
    assert body["time_range"] == recency
    assert body["include_domains"] == ["example.com"]
    assert body["include_domains_mode"] == "filter"
    assert "site:" not in body["query"]


@pytest.mark.asyncio
async def test_tavily_excludes_domains_natively_and_after(tmp_path: Path) -> None:
    data, request = await _search(
        tmp_path,
        "tavily",
        {"query": "vbot", "exclude_domains": ["reddit.com"]},
        [
            {"title": "Kept", "url": "https://example.com/a", "content": "ok"},
            {"title": "Leak", "url": "https://www.reddit.com/r/x", "content": "no"},
        ],
    )

    assert data == {
        "excluded_domains": "reddit.com",
        "content": "1. Kept\nhttps://example.com/a\nok",
    }
    body = request_json(request)
    assert body["exclude_domains"] == ["reddit.com"]
    assert body["query"] == "vbot"


@pytest.mark.asyncio
async def test_exa_gets_highlights_and_lists_the_results(tmp_path: Path) -> None:
    data, request = await _search(
        tmp_path,
        "exa",
        {"query": "vbot", "count": 5},
        [
            {
                "title": "vBot docs",
                "url": "https://example.com/vbot",
                "publishedDate": "2026-08-20T00:00:00.000Z",
                "highlights": ["vBot documentation", "agent harness"],
            },
            {"title": "vBot project", "url": "https://example.com/project"},
        ],
    )

    assert data == {
        "content": (
            "1. vBot docs\nhttps://example.com/vbot\n"
            "2026-08-20 - vBot documentation agent harness\n\n"
            "2. vBot project\nhttps://example.com/project"
        )
    }
    assert request.headers["x-api-key"] == API_KEY
    body = request_json(request)
    assert body["query"] == "vbot"
    assert body["numResults"] == 5
    assert body["contents"] == {"highlights": True}
    assert "startPublishedDate" not in body
    assert "includeDomains" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recency", "window_days"), [("day", 1), ("week", 7), ("month", 30), ("year", 365)]
)
async def test_exa_recency_is_a_publication_cutoff_and_domains_are_native(
    tmp_path: Path, recency: str, window_days: int
) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    data, request = await _search(
        tmp_path,
        "exa",
        {"query": "vbot", "domains": ["example.com"], "recency": recency},
        [
            {"title": "On-domain result", "url": "https://example.com/vbot"},
            {"title": "Off-domain leak", "url": "https://other.test/vbot"},
        ],
    )
    after = datetime.now(UTC)

    assert data["recency"] == recency
    assert result_urls(data) == ["https://example.com/vbot"]
    assert data["note"] == "Exa leaves out pages without a publication date when recency is set."
    body = request_json(request)
    assert body["includeDomains"] == ["example.com"]
    assert "site:" not in body["query"]
    cutoff = datetime.strptime(body["startPublishedDate"], "%Y-%m-%dT%H:%M:%S.000Z")
    window = timedelta(days=window_days)
    assert before - window <= cutoff.replace(tzinfo=UTC) <= after - window


@pytest.mark.asyncio
async def test_exa_sends_exclusions_only_without_inclusions(tmp_path: Path) -> None:
    registry = web_search_registry({"provider": "exa"})
    context = make_context(tmp_path)
    with respx.mock() as router:
        route = router.post(EXA_ENDPOINT).respond(200, json={"results": []})
        await registry.dispatch(context, {"query": "vbot", "exclude_domains": ["reddit.com"]})
        await registry.dispatch(
            context,
            {"query": "vbot", "domains": ["example.com"], "exclude_domains": ["blog.example.com"]},
        )

    only_excluded, both = (request_json(call.request) for call in route.calls)
    assert only_excluded["excludeDomains"] == ["reddit.com"]
    assert "includeDomains" not in only_excluded
    assert both["includeDomains"] == ["example.com"]
    assert "excludeDomains" not in both
