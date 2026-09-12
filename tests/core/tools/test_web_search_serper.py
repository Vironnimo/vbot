"""Web search: serper behavior."""

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
    _SERPER_ENDPOINT,
    _fake_credential_resolver,
    _read_json_body,
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
)


def _serper_organic(start: int, end: int) -> list[dict[str, Any]]:
    return [
        {
            "title": f"Result {index}",
            "link": f"https://example.com/{index}",
            "snippet": f"Snippet {index}",
            "position": index,
        }
        for index in range(start, end)
    ]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_serper_success_maps_results(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_SERPER_ENDPOINT).mock(
        return_value=httpx.Response(
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
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 5},
        _fake_credential_resolver,
        lambda: {"provider": "serper"},
    )

    data = assert_success_envelope(result)
    assert data["provider"] == "serper"
    assert len(data["results"]) == 2
    assert "recency" not in data
    assert "warnings" not in data
    first, second = data["results"]
    assert (first["rank"], second["rank"]) == (1, 2)
    assert first["url"] == "https://example.com/vbot"
    assert first["description"] == "vBot documentation"
    assert first["page_age"] == "Aug 20, 2026"
    assert "page_age" not in second

    request = route.calls[0].request
    assert request.headers["x-api-key"] == "test-brave-api-key"
    body = _read_json_body(request)
    assert body["q"] == "vbot"
    assert body["num"] == 5
    assert body["page"] == 1
    assert "tbs" not in body


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recency", "tbs"), [("day", "qdr:d"), ("month", "qdr:m"), ("year", "qdr:y")]
)
async def test_web_search_handler_serper_recency_and_domains(
    tmp_path: Path, recency: str, tbs: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_SERPER_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "On-domain result",
                        "link": "https://example.com/vbot",
                        "snippet": "Matching",
                    },
                    {
                        "title": "Off-domain leak",
                        "link": "https://other.test/vbot",
                        "snippet": "Must be removed",
                    },
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": ["example.com"], "recency": recency},
        _fake_credential_resolver,
        lambda: {"provider": "serper"},
    )

    data = assert_success_envelope(result)
    assert data["recency"] == recency
    assert len(data["results"]) == 1
    assert data["results"][0]["url"] == "https://example.com/vbot"

    body = _read_json_body(route.calls[0].request)
    assert body["tbs"] == tbs
    assert "site:example.com" in body["q"]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_serper_fans_out_over_ten_result_pages(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_SERPER_ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json={"organic": _serper_organic(1, 11)}),
            httpx.Response(200, json={"organic": _serper_organic(11, 14)}),
        ]
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 12},
        _fake_credential_resolver,
        lambda: {"provider": "serper"},
    )

    data = assert_success_envelope(result)
    assert len(data["results"]) == 12
    assert [entry["rank"] for entry in data["results"]] == list(range(1, 13))
    assert data["results"][0]["url"] == "https://example.com/1"
    assert data["results"][11]["url"] == "https://example.com/12"

    assert len(route.calls) == 2
    first_body = _read_json_body(route.calls[0].request)
    assert (first_body["page"], first_body["num"]) == (1, 10)
    second_body = _read_json_body(route.calls[1].request)
    assert (second_body["page"], second_body["num"]) == (2, 2)


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_serper_page_skips_into_first_serper_page(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_SERPER_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"organic": _serper_organic(1, 11)})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 5, "page": 2},
        _fake_credential_resolver,
        lambda: {"provider": "serper"},
    )

    data = assert_success_envelope(result)
    assert len(data["results"]) == 5
    assert [entry["url"] for entry in data["results"]] == [
        f"https://example.com/{index}" for index in range(6, 11)
    ]
    assert [entry["rank"] for entry in data["results"]] == [1, 2, 3, 4, 5]
    assert len(route.calls) == 1
    body = _read_json_body(route.calls[0].request)
    assert body["page"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_serper_short_page_stops_fan_out(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_SERPER_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"organic": _serper_organic(1, 4)})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 10},
        _fake_credential_resolver,
        lambda: {"provider": "serper"},
    )

    data = assert_success_envelope(result)
    assert len(data["results"]) == 3
    assert len(route.calls) == 1


@pytest.mark.asyncio
async def test_web_search_handler_serper_missing_api_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        lambda key: "",
        lambda: {"provider": "serper"},
    )

    error = assert_failure_envelope(result, "missing_api_key")
    assert "SERPER_API_KEY" in error["message"]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_serper_forbidden_hints_at_api_key(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.post(_SERPER_ENDPOINT).mock(
        return_value=httpx.Response(403, json={"message": "invalid key"})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "serper"},
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert "SERPER_API_KEY" in error["message"]
    assert error["retryable"] is False
    assert len(route.calls) == 1
