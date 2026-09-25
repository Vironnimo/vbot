"""Web search: tavily exa behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

import core.tools._web_search_transport as web_search_transport
from core.tools.web_search import (
    web_search_handler,
)
from tests.core.tools.web_search_helpers import (
    _EXA_ENDPOINT,
    _TAVILY_ENDPOINT,
    _fake_credential_resolver,
    _read_json_body,
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
    result_urls,
)


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_tavily_success_maps_results(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_TAVILY_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "vBot docs",
                        "url": "https://example.com/vbot",
                        "content": "vBot documentation",
                        "published_date": "2026-08-20",
                    },
                    {
                        "title": "vBot project",
                        "url": "https://example.com/project",
                        "content": "Project page",
                    },
                    {"title": "", "url": "", "content": ""},
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 5},
        _fake_credential_resolver,
        lambda: {"provider": "tavily"},
    )

    data = assert_success_envelope(result)
    assert data == {
        "content": (
            "1. vBot docs\nhttps://example.com/vbot\n2026-08-20 - vBot documentation\n\n"
            "2. vBot project\nhttps://example.com/project\nProject page"
        )
    }

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer test-brave-api-key"
    body = _read_json_body(request)
    assert body["query"] == "vbot"
    assert body["max_results"] == 5
    assert body["search_depth"] == "basic"
    assert body["include_answer"] is False
    assert "time_range" not in body
    assert "include_domains" not in body


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("recency", ["day", "week", "month", "year"])
async def test_web_search_handler_tavily_recency_and_domains(tmp_path: Path, recency: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_TAVILY_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "On-domain result",
                        "url": "https://example.com/vbot",
                        "content": "Matching",
                    },
                    {
                        "title": "Off-domain leak",
                        "url": "https://other.test/vbot",
                        "content": "Must be removed",
                    },
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": ["example.com"], "recency": recency},
        _fake_credential_resolver,
        lambda: {"provider": "tavily"},
    )

    data = assert_success_envelope(result)
    assert data["recency"] == recency
    assert result_urls(data) == ["https://example.com/vbot"]

    body = _read_json_body(route.calls[0].request)
    assert body["time_range"] == recency
    assert body["include_domains"] == ["example.com"]
    assert body["include_domains_mode"] == "filter"
    assert "site:" not in body["query"]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_tavily_page_warns_without_paging(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_TAVILY_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": 2},
        _fake_credential_resolver,
        lambda: {"provider": "tavily"},
    )

    data = assert_success_envelope(result)
    assert data["note"] == (
        "Tavily cannot page results; these are the first results again, not page 2."
    )
    assert len(route.calls) == 1
    assert "page" not in _read_json_body(route.calls[0].request)


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_tavily_excludes_domains_natively_and_after(
    tmp_path: Path,
) -> None:
    route = respx.post(_TAVILY_ENDPOINT).respond(
        200,
        json={
            "results": [
                {"title": "Kept", "url": "https://example.com/a", "content": "ok"},
                {"title": "Leak", "url": "https://www.reddit.com/r/x", "content": "no"},
            ]
        },
    )

    result = await web_search_handler(
        make_context(tmp_path),
        {"query": "vbot", "exclude_domains": ["reddit.com"]},
        _fake_credential_resolver,
        lambda: {"provider": "tavily"},
    )

    data = assert_success_envelope(result)
    assert data == {
        "excluded_domains": "reddit.com",
        "content": "1. Kept\nhttps://example.com/a\nok",
    }
    body = _read_json_body(route.calls[0].request)
    assert body["exclude_domains"] == ["reddit.com"]
    assert body["query"] == "vbot"


@pytest.mark.asyncio
async def test_web_search_handler_tavily_missing_api_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        lambda key: "",
        lambda: {"provider": "tavily"},
    )

    error = assert_failure_envelope(result, "missing_api_key")
    assert "TAVILY_API_KEY" in error["message"]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_tavily_unauthorized_hints_at_api_key(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_TAVILY_ENDPOINT).mock(
        return_value=httpx.Response(401, json={"detail": "Invalid API key"})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "tavily"},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert "TAVILY_API_KEY" in error["message"]
    assert error["retryable"] is False
    assert len(route.calls) == 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_tavily_retries_transient_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr("core.tools._web_search_transport.sleep_for_retry", _fake_sleep)

    route = respx.post(_TAVILY_ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, json={"detail": "rate limited"}),
            httpx.Response(200, json={"results": []}),
        ]
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "tavily"},
    )

    assert_success_envelope(result)
    assert len(route.calls) == 2


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_tavily_does_not_retry_post_500(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_TAVILY_ENDPOINT).mock(
        return_value=httpx.Response(500, json={"detail": "upstream error"})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "tavily"},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["retryable"] is False
    assert len(route.calls) == 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_tavily_rejects_oversized_post_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(web_search_transport, "_MAX_RESPONSE_BYTES", 5)
    respx.post(_TAVILY_ENDPOINT).mock(
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
        lambda: {"provider": "tavily"},
    )

    error = assert_failure_envelope(result, "response_too_large")
    assert "5 MB" in error["message"]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_exa_success_maps_results(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_EXA_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "vBot docs",
                        "url": "https://example.com/vbot",
                        "publishedDate": "2026-08-20T00:00:00.000Z",
                        "highlights": ["vBot documentation", "agent harness"],
                    },
                    {
                        "title": "vBot project",
                        "url": "https://example.com/project",
                    },
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 5},
        _fake_credential_resolver,
        lambda: {"provider": "exa"},
    )

    data = assert_success_envelope(result)
    assert data == {
        "content": (
            "1. vBot docs\nhttps://example.com/vbot\n"
            "2026-08-20 - vBot documentation agent harness\n\n"
            "2. vBot project\nhttps://example.com/project"
        )
    }

    request = route.calls[0].request
    assert request.headers["x-api-key"] == "test-brave-api-key"
    body = _read_json_body(request)
    assert body["query"] == "vbot"
    assert body["numResults"] == 5
    assert body["contents"] == {"highlights": True}
    assert "startPublishedDate" not in body
    assert "includeDomains" not in body


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recency", "window_days"), [("day", 1), ("week", 7), ("month", 30), ("year", 365)]
)
async def test_web_search_handler_exa_recency_and_domains(
    tmp_path: Path, recency: str, window_days: int
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_EXA_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "On-domain result",
                        "url": "https://example.com/vbot",
                        "highlights": ["Matching"],
                    },
                    {
                        "title": "Off-domain leak",
                        "url": "https://other.test/vbot",
                        "highlights": ["Must be removed"],
                    },
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": ["example.com"], "recency": recency},
        _fake_credential_resolver,
        lambda: {"provider": "exa"},
    )

    data = assert_success_envelope(result)
    assert data["recency"] == recency
    assert result_urls(data) == ["https://example.com/vbot"]
    assert data["note"] == "Exa leaves out pages without a publication date when recency is set."

    body = _read_json_body(route.calls[0].request)
    assert body["includeDomains"] == ["example.com"]
    assert "site:" not in body["query"]
    cutoff = datetime.strptime(body["startPublishedDate"], "%Y-%m-%dT%H:%M:%S.000Z")
    cutoff = cutoff.replace(tzinfo=UTC)
    age = datetime.now(UTC) - cutoff
    assert timedelta(days=window_days) <= age <= timedelta(days=window_days, minutes=5)


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_exa_page_warns_without_paging(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_EXA_ENDPOINT).mock(return_value=httpx.Response(200, json={"results": []}))

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": 2},
        _fake_credential_resolver,
        lambda: {"provider": "exa"},
    )

    data = assert_success_envelope(result)
    assert data["note"] == "Exa cannot page results; these are the first results again, not page 2."
    assert len(route.calls) == 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_exa_sends_exclusions_only_without_inclusions(
    tmp_path: Path,
) -> None:
    route = respx.post(_EXA_ENDPOINT).respond(200, json={"results": []})
    context = make_context(tmp_path)

    await web_search_handler(
        context,
        {"query": "vbot", "exclude_domains": ["reddit.com"]},
        _fake_credential_resolver,
        lambda: {"provider": "exa"},
    )
    await web_search_handler(
        context,
        {"query": "vbot", "domains": ["example.com"], "exclude_domains": ["blog.example.com"]},
        _fake_credential_resolver,
        lambda: {"provider": "exa"},
    )

    only_excluded, both = (_read_json_body(call.request) for call in route.calls)
    assert only_excluded["excludeDomains"] == ["reddit.com"]
    assert "includeDomains" not in only_excluded
    assert both["includeDomains"] == ["example.com"]
    assert "excludeDomains" not in both


@pytest.mark.asyncio
async def test_web_search_handler_exa_missing_api_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        lambda key: "",
        lambda: {"provider": "exa"},
    )

    error = assert_failure_envelope(result, "missing_api_key")
    assert "EXA_API_KEY" in error["message"]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_exa_unauthorized_hints_at_api_key(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_EXA_ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": "invalid api key"})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "exa"},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert "EXA_API_KEY" in error["message"]
    assert error["retryable"] is False
    assert len(route.calls) == 1
