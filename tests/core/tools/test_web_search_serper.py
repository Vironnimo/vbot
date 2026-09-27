"""web_search with Serper: request body, filters, and fan-out over its ten-result pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tests.core.tools.web_search_test_support import (
    API_KEY,
    SERPER_ENDPOINT,
    assert_success_envelope,
    request_json,
    result_urls,
    search,
)


def _organic(start: int, end: int) -> list[dict[str, Any]]:
    return [
        {
            "title": f"Result {index}",
            "link": f"https://example.com/{index}",
            "snippet": f"Snippet {index}",
            "position": index,
        }
        for index in range(start, end)
    ]


@pytest.mark.asyncio
async def test_search_sends_the_key_and_lists_the_results(tmp_path: Path) -> None:
    with respx.mock() as router:
        route = router.post(SERPER_ENDPOINT).respond(
            200,
            json={
                "organic": [
                    {
                        "title": "vBot docs",
                        "link": "https://example.com/vbot",
                        "snippet": "vBot documentation",
                        "date": "Aug 20, 2026",
                        "position": 1,
                    },
                    {
                        "title": "vBot project",
                        "link": "https://example.com/project",
                        "snippet": "Project page",
                        "position": 2,
                    },
                ]
            },
        )
        result = await search(tmp_path, {"query": "vbot", "count": 5}, provider="serper")

    assert assert_success_envelope(result) == {
        "content": (
            "1. vBot docs\nhttps://example.com/vbot\nAug 20, 2026 - vBot documentation\n\n"
            "2. vBot project\nhttps://example.com/project\nProject page"
        )
    }
    request = route.calls[0].request
    assert request.headers["x-api-key"] == API_KEY
    body = request_json(request)
    assert (body["q"], body["num"], body["page"]) == ("vbot", 5, 1)
    assert "tbs" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recency", "tbs"),
    [("day", "qdr:d"), ("week", "qdr:w"), ("month", "qdr:m"), ("year", "qdr:y")],
)
async def test_recency_is_native_and_domains_are_site_operators(
    tmp_path: Path, recency: str, tbs: str
) -> None:
    with respx.mock() as router:
        route = router.post(SERPER_ENDPOINT).respond(
            200,
            json={
                "organic": [
                    {"title": "On-domain result", "link": "https://example.com/vbot"},
                    {"title": "Off-domain leak", "link": "https://other.test/vbot"},
                ]
            },
        )
        result = await search(
            tmp_path,
            {"query": "vbot", "domains": ["example.com"], "recency": recency},
            provider="serper",
        )

    data = assert_success_envelope(result)
    assert data["recency"] == recency
    assert result_urls(data) == ["https://example.com/vbot"]
    body = request_json(route.calls[0].request)
    assert body["tbs"] == tbs
    assert "site:example.com" in body["q"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "pages", "requests", "numbers"),
    [
        # More than ten results take a second Serper page.
        (
            {"count": 12},
            [_organic(1, 11), _organic(11, 14)],
            [(1, 10), (2, 2)],
            list(range(1, 13)),
        ),
        # A later web_search page may lie inside the first Serper page.
        ({"count": 5, "page": 2}, [_organic(1, 11)], [(1, 10)], list(range(6, 11))),
        # A short Serper page ends the fan-out.
        ({"count": 10}, [_organic(1, 4)], [(1, 10)], list(range(1, 4))),
    ],
    ids=["second-page", "offset-in-first-page", "short-page"],
)
async def test_results_are_collected_over_serper_pages(
    tmp_path: Path,
    arguments: dict[str, Any],
    pages: list[list[dict[str, Any]]],
    requests: list[tuple[int, int]],
    numbers: list[int],
) -> None:
    with respx.mock() as router:
        route = router.post(SERPER_ENDPOINT).mock(
            side_effect=[httpx.Response(200, json={"organic": page}) for page in pages]
        )
        result = await search(tmp_path, {"query": "vbot", **arguments}, provider="serper")

    data = assert_success_envelope(result)
    assert result_urls(data) == [f"https://example.com/{number}" for number in numbers]
    sent = [request_json(call.request) for call in route.calls]
    assert [(body["page"], body["num"]) for body in sent] == requests
