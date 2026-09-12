"""Web search: duckduckgo perplexity behavior."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

import core.tools._web_search_providers as web_search_providers
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
    assert data["provider"] == "duckduckgo"
    assert "query" not in data
    assert "count" not in data
    assert "page" not in data
    assert len(data["results"]) == 3
    assert "warnings" not in data
    assert "recency" not in data
    results = data["results"]
    assert isinstance(results, list)
    assert len(results) == 3
    first = results[0]
    assert first["rank"] == 1
    assert first["title"] == "vBot docs"
    assert first["url"] == "https://example.com/vbot"
    assert first["description"] == "vBot & documentation for agents"
    assert "content_trust" not in first
    third = results[2]
    assert third["rank"] == 3
    assert third["url"] == "https://example.org/direct"
    assert third["description"] == ""


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
    assert len(data["results"]) == 1
    results = data["results"]
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["rank"] == 3
    assert results[0]["url"] == "https://example.org/direct"
    assert "recency" not in data
    assert data["warnings"] == [
        web_search_providers._DUCKDUCKGO_RECENCY_WARNING,
        web_search_providers._DUCKDUCKGO_PAGINATION_WARNING,
    ]


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
    assert data["applied_domains"] == ["example.com"]
    assert len(data["results"]) == 2


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
    assert "challenge" in error["message"]
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
    assert "rate-limited" in error["message"]
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
    assert data["provider"] == "perplexity"
    assert "query" not in data
    assert "count" not in data
    assert "page" not in data
    assert data["recency"] == "month"
    assert data["applied_domains"] == ["example.com"]
    assert len(data["results"]) == 2
    assert "warnings" not in data
    results = data["results"]
    assert isinstance(results, list)
    assert len(results) == 2
    first = results[0]
    assert first["rank"] == 1
    assert first["title"] == "vBot docs"
    assert first["url"] == "https://example.com/vbot"
    assert first["description"] == "vBot documentation"
    assert first["page_age"] == "2026-08-20"
    assert results[1]["page_age"] == "2026-09-01"


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
    assert len(data["results"]) == 0
    assert data["warnings"] == [web_search_providers._PERPLEXITY_PAGINATION_WARNING]


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
    assert "PERPLEXITY_API_KEY" in error["message"]


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
    assert "PERPLEXITY_API_KEY" in error["message"]
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
    assert data["results"] == [
        {
            "rank": 1,
            "title": "A <b>literal</b> & title",
            "url": "https://example.com/one",
            "description": "x < y and z > w works",
        },
        {"rank": 2, "title": "Second", "url": "https://example.com/two", "description": ""},
    ]
