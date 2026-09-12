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
    assert data["provider"] == "tavily"
    assert "query" not in data
    assert "count" not in data
    assert "page" not in data
    assert len(data["results"]) == 2
    assert "recency" not in data
    assert "warnings" not in data
    first, second = data["results"]
    assert (first["rank"], second["rank"]) == (1, 2)
    assert first["description"] == "vBot documentation"
    assert first["page_age"] == "2026-08-20"
    assert "page_age" not in second

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
@pytest.mark.parametrize("recency", ["day", "month", "year"])
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
    assert len(data["results"]) == 1
    assert data["results"][0]["url"] == "https://example.com/vbot"

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
    assert "page" not in data
    warnings = data.get("warnings", [])
    assert any("paging" in warning for warning in warnings), (
        f"expected a pagination warning, got {warnings}"
    )
    assert len(route.calls) == 1
    assert "page" not in _read_json_body(route.calls[0].request)


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
    assert data["provider"] == "exa"
    assert len(data["results"]) == 2
    assert "recency" not in data
    assert "warnings" not in data
    first, second = data["results"]
    assert (first["rank"], second["rank"]) == (1, 2)
    assert first["description"] == "vBot documentation agent harness"
    assert first["page_age"] == "2026-08-20T00:00:00.000Z"
    assert second["description"] == ""
    assert "page_age" not in second

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
@pytest.mark.parametrize(("recency", "window_days"), [("day", 1), ("month", 30), ("year", 365)])
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
    assert len(data["results"]) == 1
    warnings = data.get("warnings", [])
    assert any("published date" in warning for warning in warnings), (
        f"expected a recency warning, got {warnings}"
    )

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
    assert "page" not in data
    warnings = data.get("warnings", [])
    assert any("paging" in warning for warning in warnings), (
        f"expected a pagination warning, got {warnings}"
    )
    assert len(route.calls) == 1


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
