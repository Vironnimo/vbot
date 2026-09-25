"""Web search: duckduckgo perplexity behavior."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from core.tools.web_search import (
    web_search_handler,
)
from tests.core.tools.web_search_helpers import (
    _DUCKDUCKGO_ENDPOINT,
    _PERPLEXITY_ENDPOINT,
    _fake_credential_resolver,
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
    result_urls,
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


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_duckduckgo_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_DUCKDUCKGO_ENDPOINT).mock(
        return_value=httpx.Response(200, text=_DUCKDUCKGO_HTML)
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 5},
        _fake_credential_resolver,
        lambda: {"provider": "duckduckgo"},
    )

    assert route.called is True
    request = route.calls[0].request
    assert request.url.params["q"] == "vbot"
    assert request.url.params["kp"] == "-1"

    data = assert_success_envelope(result)
    assert data == {
        "content": (
            "1. vBot docs\nhttps://example.com/vbot\nvBot & documentation for agents\n\n"
            "2. vBot guide\nhttps://example.com/guide\nGetting started\n\n"
            "3. Direct link result\nhttps://example.org/direct"
        )
    }


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_duckduckgo_page_slices_client_side(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_DUCKDUCKGO_ENDPOINT).mock(return_value=httpx.Response(200, text=_DUCKDUCKGO_HTML))

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 2, "page": 2, "recency": "month"},
        _fake_credential_resolver,
        lambda: {"provider": "duckduckgo"},
    )

    data = assert_success_envelope(result)
    assert data == {
        "note": (
            "DuckDuckGo cannot limit results by age, so these results are not limited to "
            "the past month; check result dates. DuckDuckGo returns one result list; later "
            "pages only split it and may be empty."
        ),
        "content": "1. Direct link result\nhttps://example.org/direct",
    }


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_duckduckgo_domains_use_site_operator(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_DUCKDUCKGO_ENDPOINT).mock(
        return_value=httpx.Response(200, text=_DUCKDUCKGO_HTML)
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": ["example.com"]},
        _fake_credential_resolver,
        lambda: {"provider": "duckduckgo"},
    )

    assert route.calls[0].request.url.params["q"] == "vbot site:example.com"
    data = assert_success_envelope(result)
    assert data["domains"] == "example.com"
    assert result_urls(data) == ["https://example.com/vbot", "https://example.com/guide"]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_duckduckgo_challenge_is_retryable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_DUCKDUCKGO_ENDPOINT).mock(
        return_value=httpx.Response(200, text=_DUCKDUCKGO_CHALLENGE_HTML)
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "duckduckgo"},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == (
        "DuckDuckGo answered with a bot-detection challenge instead of results. Wait a while "
        "before searching again; if this keeps happening, tell the user they can choose "
        "another search provider in Settings under Web search."
    )
    assert error["retryable"] is True


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_duckduckgo_rate_limit_is_retryable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_DUCKDUCKGO_ENDPOINT).mock(return_value=httpx.Response(202, text=""))

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "duckduckgo"},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"].startswith("DuckDuckGo is limiting requests")
    assert error["retryable"] is True


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_perplexity_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_PERPLEXITY_ENDPOINT).mock(
        return_value=httpx.Response(
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
    )

    result = await web_search_handler(
        make_context(workspace),
        {
            "query": "vbot",
            "count": 5,
            "recency": "month",
            "domains": ["example.com"],
        },
        _fake_credential_resolver,
        lambda: {"provider": "perplexity"},
    )

    assert route.called is True
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-brave-api-key"
    body = json.loads(request.content.decode("utf-8"))
    assert body["query"] == "vbot"
    assert body["max_results"] == 5
    assert body["search_recency_filter"] == "month"
    assert body["search_domain_filter"] == ["example.com"]

    data = assert_success_envelope(result)
    assert data == {
        "domains": "example.com",
        "recency": "month",
        "content": (
            "1. vBot docs\nhttps://example.com/vbot\n2026-08-20 - vBot documentation\n\n"
            "2. vBot guide\nhttps://example.com/guide\n2026-09-01 - Getting started"
        ),
    }


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_perplexity_page_warns_without_paging(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.post(_PERPLEXITY_ENDPOINT).mock(return_value=httpx.Response(200, json={"results": []}))

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": 2},
        _fake_credential_resolver,
        lambda: {"provider": "perplexity"},
    )

    data = assert_success_envelope(result)
    assert data == {
        "note": "Perplexity cannot page results; these are the first results again, not page 2.",
        "content": "No results found. Try other or fewer search words.",
    }


@pytest.mark.asyncio
async def test_web_search_handler_perplexity_missing_api_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        lambda key: "",
        lambda: {"provider": "perplexity"},
    )

    error = assert_failure_envelope(result, "missing_api_key")
    assert error["message"] == (
        "Web search is not set up: the selected provider, Perplexity, needs "
        "PERPLEXITY_API_KEY in the .env file of the vBot data directory. Tell the user: they "
        "can add the key, or choose another provider in Settings under Web search "
        "(DuckDuckGo needs no key)."
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_perplexity_unauthorized_hints_at_api_key(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.post(_PERPLEXITY_ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": "invalid key"})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "perplexity"},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["message"] == (
        "Perplexity rejected the API key (HTTP 401: invalid key). Tell the user to check "
        "PERPLEXITY_API_KEY in the .env file of the vBot data directory."
    )
    assert error["retryable"] is False


@respx.mock
@pytest.mark.asyncio
async def test_duckduckgo_parses_html_attributes_and_keeps_snippet_scope(tmp_path: Path) -> None:
    respx.get(_DUCKDUCKGO_ENDPOINT).respond(
        200,
        text="""
        <a class='other result__a' title='a > b' href='https://example.com/one'>
          A &lt;b&gt;literal&lt;/b&gt; &amp; <b>title</b>
        </a>
        <div class='result__snippet'>x &lt; y and z &gt; w <em>works</em></div>
        <a href=https://example.com/two class=result__a>Second</a>
        <a class='result__a'>Invalid</a>
        <span class=result__snippet>Must not attach to Second</span>
    """,
    )
    result = await web_search_handler(
        make_context(tmp_path),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "duckduckgo"},
    )
    data = assert_success_envelope(result)
    assert data["content"] == (
        "1. A <b>literal</b> & title\nhttps://example.com/one\nx < y and z > w works\n\n"
        "2. Second\nhttps://example.com/two"
    )
