"""web_search with DuckDuckGo (keyless HTML results) and Perplexity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import respx

from tests.core.tools.web_search_test_support import (
    API_KEY,
    DUCKDUCKGO_ENDPOINT,
    PERPLEXITY_ENDPOINT,
    assert_failure_envelope,
    assert_success_envelope,
    request_json,
    result_urls,
    search,
)

_DUCKDUCKGO_HTML = """<html><body>
<div class="result">
<a rel="nofollow" class="result__a"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fvbot">vBot <b>docs</b></a>
<a class="result__snippet"
href="https://example.com/vbot">vBot &amp; documentation for agents</a>
</div>
<div class="result">
<a rel="nofollow" class="result__a"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide">vBot guide</a>
<a class="result__snippet"
href="https://example.com/guide">Getting started</a>
</div>
<div class="result">
<a rel="nofollow" class="result__a" href="https://example.org/direct">Direct link result</a>
</div>
</body></html>"""

_DUCKDUCKGO_CHALLENGE_HTML = """<html><body>
<form id="challenge-form" action="/challenge" method="post">
<p>are you a human? complete the challenge below</p>
<div class="g-recaptcha"></div>
</form>
</body></html>"""


async def _search_duckduckgo(
    tmp_path: Path, arguments: dict[str, Any], status: int = 200, html: str = _DUCKDUCKGO_HTML
) -> tuple[dict[str, Any], respx.Route]:
    with respx.mock() as router:
        route = router.get(DUCKDUCKGO_ENDPOINT).respond(status, text=html)
        result = await search(tmp_path, arguments, provider="duckduckgo")
    return result, route


@pytest.mark.asyncio
async def test_duckduckgo_results_are_read_from_its_html_page(tmp_path: Path) -> None:
    result, route = await _search_duckduckgo(tmp_path, {"query": "vbot", "count": 5})

    request = route.calls[0].request
    assert request.url.params["q"] == "vbot"
    assert request.url.params["kp"] == "-1"
    assert assert_success_envelope(result) == {
        "content": (
            "1. vBot docs\nhttps://example.com/vbot\nvBot & documentation for agents\n\n"
            "2. vBot guide\nhttps://example.com/guide\nGetting started\n\n"
            "3. Direct link result\nhttps://example.org/direct"
        )
    }


@pytest.mark.asyncio
async def test_duckduckgo_parses_html_attributes_and_keeps_snippet_scope(tmp_path: Path) -> None:
    html = """
        <a class='other result__a' title='a > b' href='https://example.com/one'>
          A &lt;b&gt;literal&lt;/b&gt; &amp; <b>title</b>
        </a>
        <div class='result__snippet'>x &lt; y and z &gt; w <em>works</em></div>
        <a href=https://example.com/two class=result__a>Second</a>
        <a class='result__a'>Invalid</a>
        <span class=result__snippet>Must not attach to Second</span>
    """

    result, _ = await _search_duckduckgo(tmp_path, {"query": "vbot"}, html=html)

    assert assert_success_envelope(result)["content"] == (
        "1. A <b>literal</b> & title\nhttps://example.com/one\nx < y and z > w works\n\n"
        "2. Second\nhttps://example.com/two"
    )


@pytest.mark.asyncio
async def test_duckduckgo_pages_split_its_one_list_and_recency_is_not_applied(
    tmp_path: Path,
) -> None:
    result, _ = await _search_duckduckgo(
        tmp_path, {"query": "vbot", "count": 2, "page": 2, "recency": "month"}
    )

    assert assert_success_envelope(result) == {
        "note": (
            "DuckDuckGo cannot limit results by age, so these results are not limited to "
            "the past month; check result dates. DuckDuckGo returns one result list; later "
            "pages only split it and may be empty."
        ),
        "content": "1. Direct link result\nhttps://example.org/direct",
    }


@pytest.mark.asyncio
async def test_duckduckgo_domains_use_the_site_operator(tmp_path: Path) -> None:
    result, route = await _search_duckduckgo(
        tmp_path, {"query": "vbot", "domains": ["example.com"]}
    )

    assert route.calls[0].request.url.params["q"] == "vbot site:example.com"
    data = assert_success_envelope(result)
    assert data["domains"] == "example.com"
    assert result_urls(data) == ["https://example.com/vbot", "https://example.com/guide"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "html", "message"),
    [
        (
            200,
            _DUCKDUCKGO_CHALLENGE_HTML,
            "DuckDuckGo answered with a bot-detection challenge instead of results. Wait a "
            "while before searching again; if this keeps happening, tell the user they can "
            "choose another search provider in Settings under Web search.",
        ),
        (202, "", "DuckDuckGo is limiting requests"),
    ],
    ids=["challenge", "rate-limit"],
)
async def test_duckduckgo_bot_checks_are_retryable_failures(
    tmp_path: Path, status: int, html: str, message: str
) -> None:
    result, _ = await _search_duckduckgo(tmp_path, {"query": "vbot"}, status=status, html=html)

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"].startswith(message)
    assert error["retryable"] is True


@pytest.mark.asyncio
async def test_perplexity_gets_native_filters_and_lists_dated_results(tmp_path: Path) -> None:
    with respx.mock() as router:
        route = router.post(PERPLEXITY_ENDPOINT).respond(
            200,
            json={
                "results": [
                    {
                        "title": "vBot docs",
                        "url": "https://example.com/vbot",
                        "snippet": "vBot documentation",
                        "date": "2026-08-20",
                    },
                    {
                        "title": "vBot guide",
                        "url": "https://example.com/guide",
                        "snippet": "Getting started",
                        "last_updated": "2026-09-01",
                    },
                ]
            },
        )
        result = await search(
            tmp_path,
            {"query": "vbot", "count": 5, "recency": "month", "domains": ["example.com"]},
            provider="perplexity",
        )

    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    body = request_json(request)
    assert body["query"] == "vbot"
    assert body["max_results"] == 5
    assert body["search_recency_filter"] == "month"
    assert body["search_domain_filter"] == ["example.com"]
    assert assert_success_envelope(result) == {
        "domains": "example.com",
        "recency": "month",
        "content": (
            "1. vBot docs\nhttps://example.com/vbot\n2026-08-20 - vBot documentation\n\n"
            "2. vBot guide\nhttps://example.com/guide\n2026-09-01 - Getting started"
        ),
    }
