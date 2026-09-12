"""Web search: firecrawl behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.tools.web_search import (
    web_search_handler,
)
from tests.core.tools.web_search_helpers import (
    _FIRECRAWL_ENDPOINT,
    _fake_credential_resolver,
    _read_json_body,
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
)


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_firecrawl_success_maps_results(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
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
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 5},
        _fake_credential_resolver,
        lambda: {"provider": "firecrawl"},
    )

    data = assert_success_envelope(result)
    assert data["provider"] == "firecrawl"
    assert len(data["results"]) == 2
    assert "recency" not in data
    assert "warnings" not in data
    first, second = data["results"]
    assert (first["rank"], second["rank"]) == (1, 2)
    assert first["description"] == "vBot documentation"
    assert "page_age" not in first

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer test-brave-api-key"
    body = _read_json_body(request)
    assert body["query"] == "vbot"
    assert body["limit"] == 5
    assert body["sources"] == ["web"]
    assert "tbs" not in body
    assert "includeDomains" not in body


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recency", "tbs"), [("day", "qdr:d"), ("month", "qdr:m"), ("year", "qdr:y")]
)
async def test_web_search_handler_firecrawl_recency_and_domains(
    tmp_path: Path, recency: str, tbs: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "On-domain result",
                            "url": "https://example.com/vbot",
                            "description": "Matching",
                        },
                        {
                            "title": "Off-domain leak",
                            "url": "https://other.test/vbot",
                            "description": "Must be removed",
                        },
                    ]
                },
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": ["example.com"], "recency": recency},
        _fake_credential_resolver,
        lambda: {"provider": "firecrawl"},
    )

    data = assert_success_envelope(result)
    assert data["recency"] == recency
    assert len(data["results"]) == 1
    assert data["results"][0]["url"] == "https://example.com/vbot"

    body = _read_json_body(route.calls[0].request)
    assert body["tbs"] == tbs
    assert body["includeDomains"] == ["example.com"]
    assert "site:" not in body["query"]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_firecrawl_page_warns_without_paging(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"web": []}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": 2},
        _fake_credential_resolver,
        lambda: {"provider": "firecrawl"},
    )

    data = assert_success_envelope(result)
    assert "page" not in data
    warnings = data.get("warnings", [])
    assert any("paging" in warning for warning in warnings), (
        f"expected a pagination warning, got {warnings}"
    )
    assert len(route.calls) == 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_firecrawl_failed_envelope_is_a_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(
            200, json={"success": False, "error": "concurrency limit reached"}
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "firecrawl"},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert "concurrency limit reached" in error["message"]
    assert error["retryable"] is False
    assert len(route.calls) == 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_firecrawl_retries_gateway_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr("core.tools._web_search_transport.sleep_for_retry", _fake_sleep)

    route = respx.post(_FIRECRAWL_ENDPOINT).mock(
        side_effect=[
            httpx.Response(408, json={"success": False, "error": "timed out"}),
            httpx.Response(200, json={"success": True, "data": {"web": []}}),
        ]
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "firecrawl"},
    )

    assert_success_envelope(result)
    assert len(route.calls) == 2


@pytest.mark.asyncio
async def test_web_search_handler_firecrawl_missing_api_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        lambda key: "",
        lambda: {"provider": "firecrawl"},
    )

    error = assert_failure_envelope(result, "missing_api_key")
    assert "FIRECRAWL_API_KEY" in error["message"]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_firecrawl_unauthorized_hints_at_api_key(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(401, json={"success": False, "error": "unauthorized"})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "firecrawl"},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert "FIRECRAWL_API_KEY" in error["message"]
    assert error["retryable"] is False
    assert len(route.calls) == 1


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("envelope", [{"results": []}, {"data": []}])
async def test_web_search_handler_firecrawl_accepts_alternate_envelopes(
    tmp_path: Path, envelope: dict[str, Any]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    item = {
        "title": "vBot docs",
        "url": "https://example.com/vbot",
        "description": "vBot documentation",
    }
    key = "results" if "results" in envelope else "data"
    respx.post(_FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"success": True, **{key: [item]}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "firecrawl"},
    )

    data = assert_success_envelope(result)
    assert len(data["results"]) == 1
    assert data["results"][0]["url"] == "https://example.com/vbot"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_firecrawl_maps_fallback_item_fields(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.post(_FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
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
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "firecrawl"},
    )

    data = assert_success_envelope(result)
    assert len(data["results"]) == 1
    (entry,) = data["results"]
    assert entry["title"] == "vBot docs"
    assert entry["url"] == "https://example.com/vbot"
    assert entry["description"] == "vBot documentation"
    assert entry["page_age"] == "2026-08-20"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_firecrawl_failed_envelope_reads_message_field(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.post(_FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"success": False, "message": "too many requests"})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "firecrawl"},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert "too many requests" in error["message"]
    assert error["retryable"] is False
