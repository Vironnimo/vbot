"""Web search: brave behavior."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

import core.tools._web_search_transport as web_search_transport
from core.tools.web_search import (
    web_search_handler,
)
from tests.core.tools.web_search_helpers import (
    _BRAVE_ENDPOINT,
    _SEARXNG_ENDPOINT,
    _fake_credential_resolver,
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
    result_urls,
)


class _FailIfReadStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError("oversized declared response body must not be read")
        yield b""  # pragma: no cover


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "vBot docs",
                            "url": "https://example.com/vbot",
                            "description": "vBot documentation",
                        }
                    ]
                }
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 5},
        _fake_credential_resolver,
    )

    assert route.called is True
    request = route.calls[0].request
    assert request.headers["X-Subscription-Token"] == "test-brave-api-key"
    assert request.headers["Accept"] == "application/json"
    assert request.url.params["q"] == "vbot"
    assert request.url.params["count"] == "5"

    data = assert_success_envelope(result)
    assert data == {"content": "1. vBot docs\nhttps://example.com/vbot\nvBot documentation"}


@respx.mock
@pytest.mark.asyncio
async def test_web_search_rejects_declared_oversize_before_reading_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(web_search_transport, "_MAX_RESPONSE_BYTES", 5)
    respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            headers={"content-length": "6"},
            stream=_FailIfReadStream(),
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "response_too_large")
    assert error["retryable"] is False
    assert error["message"] == (
        "The search provider's response exceeds the 5 MB limit. Try again with a lower count."
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_rejects_searxng_body_larger_than_declared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(web_search_transport, "_MAX_RESPONSE_BYTES", 5)
    respx.get(_SEARXNG_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            headers={"content-length": "1"},
            content=b"123456",
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    error = assert_failure_envelope(result, "response_too_large")
    assert error["retryable"] is False


@respx.mock
@pytest.mark.asyncio
async def test_web_search_accepts_response_at_exact_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    body = b'{"web":{"results":[]}}'
    monkeypatch.setattr(web_search_transport, "_MAX_RESPONSE_BYTES", len(body))
    respx.get(_BRAVE_ENDPOINT).mock(return_value=httpx.Response(200, content=body))

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data == {"content": "No results found. Try other or fewer search words."}


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_applies_and_enforces_domains(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Root docs",
                            "url": "https://example.com/docs",
                            "description": "Root domain",
                        },
                        {
                            "title": "Subdomain docs",
                            "url": "https://docs.example.com/vbot",
                            "description": "Included subdomain",
                        },
                        {
                            "title": "Suffix attack",
                            "url": "https://example.com.evil.test/vbot",
                            "description": "Must not match",
                        },
                        {
                            "title": "Query-string mention",
                            "url": "https://other.test/?next=https://example.com",
                            "description": "Must not match",
                        },
                    ]
                }
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {
            "query": "vbot",
            "domains": ["Example.COM.", "docs.example.com", "example.com"],
        },
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data["domains"] == "example.com, docs.example.com"
    assert result_urls(data) == [
        "https://example.com/docs",
        "https://docs.example.com/vbot",
    ]
    assert route.calls[0].request.url.params["q"] == (
        "vbot site:example.com OR site:docs.example.com"
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_specific_subdomain_narrows_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Root",
                            "url": "https://example.com/vbot",
                            "description": "Excluded root",
                        },
                        {
                            "title": "WWW",
                            "url": "https://www.example.com/vbot",
                            "description": "Included subdomain",
                        },
                    ]
                }
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": ["www.example.com"]},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data["domains"] == "www.example.com"
    assert result_urls(data) == ["https://www.example.com/vbot"]
    assert route.calls[0].request.url.params["q"] == "vbot site:www.example.com"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_normalizes_internationalized_domain(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Internationalized domain",
                            "url": "https://faß.example/vbot",
                            "description": "Included after IDNA normalization",
                        }
                    ]
                }
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": ["FAẞ.example."]},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data["domains"] == "xn--fa-hia.example"
    assert result_urls(data) == ["https://faß.example/vbot"]
    assert route.calls[0].request.url.params["q"] == "vbot site:xn--fa-hia.example"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_passes_query_operators_through_unchanged(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"web": {"results": []}})
    )
    query = 'vbot "agent loop" -draft filetype:pdf site:example.com/docs'

    result = await web_search_handler(
        make_context(workspace),
        {"query": query},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert "domains" not in data
    assert route.calls[0].request.url.params["q"] == query


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recency", "provider_value"),
    [("day", "pd"), ("week", "pw"), ("month", "pm"), ("year", "py")],
)
async def test_web_search_handler_brave_maps_canonical_recency(
    tmp_path: Path,
    recency: str,
    provider_value: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"web": {"results": []}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "recency": recency},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    tip = "no recency limit" if recency == "year" else "a longer recency window"
    assert data == {
        "recency": recency,
        "content": f"No results found. Try other or fewer search words, or {tip}.",
    }
    request = route.calls[0].request
    assert request.url.params["freshness"] == provider_value


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_default_count_and_no_offset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"web": {"results": []}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    assert_success_envelope(result)
    request = route.calls[0].request
    assert request.url.params["count"] == "12"
    assert request.url.params["text_decorations"] == "false"
    assert "offset" not in request.url.params


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_uses_configured_default_count(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"web": {"results": []}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "brave", "default_count": 7},
    )

    assert_success_envelope(result)
    assert route.calls[0].request.url.params["count"] == "7"


@pytest.mark.asyncio
async def test_web_search_handler_rejects_invalid_configured_default_count(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "brave", "default_count": 0},
    )

    error = assert_failure_envelope(result, "configuration_error")
    assert error["message"] == (
        "Web search settings are invalid (web_search.default_count must be an integer "
        "between 1 and 20). Tell the user to check Settings under Web search."
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_page_maps_to_offset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"web": {"results": []}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": 3},
        _fake_credential_resolver,
    )

    assert_success_envelope(result)
    assert route.calls[0].request.url.params["offset"] == "2"


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [0, 11])
async def test_web_search_handler_page_out_of_range(tmp_path: Path, page: int) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": page},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "invalid_arguments")


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_strips_markup_and_keeps_page_age(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
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
                    ]
                }
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data["content"] == (
        "1. vBot docs\nhttps://example.com/vbot\n2026-05-01 - The vBot docs & guides\n\n"
        "2. vBot news\nhttps://example.com/news\nNo date on this one"
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_domain_filter_suppresses_more_results(
    tmp_path: Path,
) -> None:
    """Brave's more_results_available counts unfiltered results, so site filters hide it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Example result",
                            "url": "https://example.com/vbot",
                            "description": "Matching",
                        }
                    ]
                },
                "query": {"more_results_available": True},
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": ["example.com"]},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data == {
        "domains": "example.com",
        "content": "1. Example result\nhttps://example.com/vbot\nMatching",
    }


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_without_domains_names_the_next_page(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {"results": [{"title": "A", "url": "https://a.example/"}]},
                "query": {"more_results_available": True},
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": 2},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data == {
        "more": "More results are available with page 3.",
        "content": "1. A\nhttps://a.example/",
    }


@respx.mock
@pytest.mark.asyncio
async def test_brave_preserves_plain_comparisons_in_decorated_snippets(tmp_path: Path) -> None:
    respx.get(_BRAVE_ENDPOINT).respond(
        200,
        json={
            "web": {
                "results": [
                    {
                        "title": "A <b title='a > b'>title</b>",
                        "url": "https://example.com",
                        "description": "x < y and z > w &amp; more",
                    }
                ]
            }
        },
    )
    result = await web_search_handler(
        make_context(tmp_path),
        {"query": "vbot"},
        _fake_credential_resolver,
    )
    data = assert_success_envelope(result)
    assert data["content"] == "1. A title\nhttps://example.com\nx < y and z > w & more"
