"""Tests for the built-in web_search tool."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import core.tools.web_search as web_search_module
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope
from core.tools.web_search import (
    WEB_SEARCH_TOOL_DESCRIPTION,
    WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_PARAMETERS,
    _resolve_web_search_settings,
    register_web_search_tool,
    web_search_handler,
)
from core.utils.retry import MAX_RETRIES

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html"
_EXA_ENDPOINT = "https://api.exa.ai/search"
_FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/search"
_SERPER_ENDPOINT = "https://google.serper.dev/search"
_SEARXNG_ENDPOINT = "http://localhost:8888/search"
_TAVILY_ENDPOINT = "https://api.tavily.com/search"


class _FailIfReadStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError("oversized declared response body must not be read")
        yield b""  # pragma: no cover


def make_context(workspace: Path, tool_name: str = WEB_SEARCH_TOOL_NAME) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=workspace.parent,
        data_root=workspace.parent / "data",
    )


def assert_success_envelope(result: dict[str, object]) -> dict[str, Any]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    return data


def assert_failure_envelope(result: dict[str, object], code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error  # type: ignore[return-value]


def _fake_credential_resolver(key: str) -> str:
    del key
    return "test-brave-api-key"


def _collect_schema_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    result: list[str] = []
    if isinstance(value, dict):
        for key, nested_value in value.items():
            result.append(str(key))
            result.extend(_collect_schema_strings(nested_value))
    elif isinstance(value, list):
        for item in value:
            result.extend(_collect_schema_strings(item))
    return result


def test_register_web_search_tool_schema() -> None:
    registry = ToolRegistry()

    register_web_search_tool(registry, lambda key: "")

    tool = registry.get("web_search")
    assert tool.name == WEB_SEARCH_TOOL_NAME == "web_search"
    assert tool.description == WEB_SEARCH_TOOL_DESCRIPTION
    assert tool.parameters == WEB_SEARCH_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["web_search"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition["name"] == "web_search"
    assert definition["description"] == WEB_SEARCH_TOOL_DESCRIPTION

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["query"]
    assert "additionalProperties" not in parameters
    assert tool.open_input_schema is True
    display = registry.display_for_call(
        "web_search",
        {
            "description": "Find the current release notes",
            "query": "vBot release notes",
        },
        result={
            "ok": True,
            "error": None,
            "data": {"result_count": 1},
            "artifacts": [],
        },
    )
    assert display["primary"][0]["value"] == "Find the current release notes"
    assert display["facts"] == [{"kind": "count", "value": 1, "unit": "results", "at_least": False}]

    properties = parameters["properties"]
    assert "provider" not in properties
    assert set(properties) == {
        "query",
        "domains",
        "count",
        "page",
        "recency",
    }
    domains_schema = properties["domains"]
    assert domains_schema["items"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 253,
    }
    assert domains_schema["minItems"] == 1
    assert domains_schema["maxItems"] == 10
    assert domains_schema["uniqueItems"] is True
    count_schema = properties["count"]
    assert count_schema["minimum"] == 1
    assert count_schema["maximum"] == 20
    page_schema = properties["page"]
    assert page_schema["minimum"] == 1
    assert page_schema["maximum"] == 10
    assert page_schema["default"] == 1
    assert properties["recency"]["enum"] == ["day", "month", "year"]
    assert isinstance(properties["recency"]["description"], str)
    assert properties["recency"]["description"]
    assert all("default" not in schema for name, schema in properties.items() if name != "page")


@pytest.mark.asyncio
async def test_web_search_handler_missing_api_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        lambda key: "",
    )

    error = assert_failure_envelope(result, "missing_api_key")
    assert "BRAVE_API_KEY" in error["message"]


@pytest.mark.asyncio
async def test_web_search_handler_empty_query(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "   "},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 21])
async def test_web_search_handler_count_out_of_range(tmp_path: Path, count: int) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": count},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
@pytest.mark.parametrize("retired_field", ["freshness", "date_after", "date_before"])
async def test_web_search_handler_rejects_retired_time_filters(
    tmp_path: Path,
    retired_field: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", retired_field: "day"},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
async def test_web_search_handler_invalid_recency(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "recency": "week"},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domains",
    [
        "",
        "example.com",
        [],
        {"domain": "example.com"},
        ["https://example.com"],
        ["example.com/docs"],
        ["*.example.com"],
        ["bad domain.example"],
        ["-example.com"],
        [1],
        [f"domain{index}.example" for index in range(11)],
    ],
)
async def test_web_search_handler_rejects_invalid_domains(
    tmp_path: Path,
    domains: Any,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": domains},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "validation_error")
    assert "domains" in error["message"]


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
    assert data["provider"] == "brave"
    assert data["query"] == "vbot"
    assert data["count_requested"] == 5
    assert data["result_count"] == 1
    assert data["content_trust"] == "untrusted_web_content"
    results = data["results"]
    assert isinstance(results, list)
    assert len(results) == 1
    first = results[0]
    assert first["rank"] == 1
    assert first["title"] == "vBot docs"
    assert first["url"] == "https://example.com/vbot"
    assert first["description"] == "vBot documentation"
    assert first["content_trust"] == "untrusted_web_content"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_rejects_declared_oversize_before_reading_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(web_search_module, "_MAX_RESPONSE_BYTES", 5)
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
    assert "5 MB" in error["message"]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_rejects_searxng_body_larger_than_declared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(web_search_module, "_MAX_RESPONSE_BYTES", 5)
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
    monkeypatch.setattr(web_search_module, "_MAX_RESPONSE_BYTES", len(body))
    respx.get(_BRAVE_ENDPOINT).mock(return_value=httpx.Response(200, content=body))

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data["result_count"] == 0


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
    assert data["query"] == "vbot"
    assert data["applied_domains"] == ["example.com", "docs.example.com"]
    assert data["result_count"] == 2
    assert [entry["url"] for entry in data["results"]] == [
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
    assert data["applied_domains"] == ["www.example.com"]
    assert [entry["url"] for entry in data["results"]] == ["https://www.example.com/vbot"]
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
    assert data["applied_domains"] == ["xn--fa-hia.example"]
    assert data["result_count"] == 1
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
    query = 'site:example.com vbot "agent loop" filetype:pdf'

    result = await web_search_handler(
        make_context(workspace),
        {"query": query},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data["query"] == query
    assert "applied_domains" not in data
    assert route.calls[0].request.url.params["q"] == query


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_http_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(403, json={"error": {"detail": "forbidden"}})
    )

    with caplog.at_level(logging.WARNING, logger="vbot.tools.web_search"):
        result = await web_search_handler(
            make_context(workspace),
            {"query": "vbot"},
            _fake_credential_resolver,
        )

    assert_failure_envelope(result, "provider_request_failed")
    assert any(
        record.levelno == logging.WARNING
        and "Brave web search request failed" in record.getMessage()
        for record in caplog.records
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_network_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    sleep_attempts: list[int] = []

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del retry_after
        sleep_attempts.append(attempt)

    monkeypatch.setattr("core.tools.web_search.sleep_for_retry", _fake_sleep)

    def _raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    route = respx.get(_BRAVE_ENDPOINT).mock(side_effect=_raise_connect_error)

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "provider_request_failed")
    assert len(route.calls) == 4
    assert sleep_attempts == [0, 1, 2]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 503])
async def test_web_search_handler_retries_transient_http_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    sleep_attempts: list[int] = []

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del retry_after
        sleep_attempts.append(attempt)

    monkeypatch.setattr("core.tools.web_search.sleep_for_retry", _fake_sleep)

    route = respx.get(_BRAVE_ENDPOINT).mock(
        side_effect=[
            httpx.Response(status_code, json={"error": {"message": "temporary failure"}}),
            httpx.Response(200, json={"web": {"results": []}}),
        ]
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data["result_count"] == 0
    assert len(route.calls) == 2
    assert sleep_attempts == [0]


@respx.mock
@pytest.mark.asyncio
async def test_web_search_brave_exhausted_status_signals_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr("core.tools.web_search.sleep_for_retry", _fake_sleep)

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(503, json={"error": {"message": "busy"}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["retryable"] is True
    assert error["attempts_made"] == MAX_RETRIES + 1
    assert len(route.calls) == MAX_RETRIES + 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_brave_network_error_signals_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr("core.tools.web_search.sleep_for_retry", _fake_sleep)

    def _raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    respx.get(_BRAVE_ENDPOINT).mock(side_effect=_raise_connect_error)

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["retryable"] is True
    assert error["attempts_made"] == MAX_RETRIES + 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_brave_non_retryable_status_signals_not_retryable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(403, json={"error": {"detail": "forbidden"}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "provider_request_failed")
    assert error["retryable"] is False
    assert "attempts_made" not in error


@pytest.mark.asyncio
async def test_web_search_validation_error_signals_not_retryable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "   "},
        _fake_credential_resolver,
    )

    error = assert_failure_envelope(result, "validation_error")
    assert error["retryable"] is False


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recency", "provider_value"),
    [("day", "pd"), ("month", "pm"), ("year", "py")],
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
    assert data["result_count"] == 0
    assert data["recency"] == recency
    assert "filters" not in data
    request = route.calls[0].request
    assert request.url.params["freshness"] == provider_value


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("recency", ["day", "month", "year"])
async def test_web_search_handler_searxng_maps_canonical_recency_without_api_key(
    tmp_path: Path,
    recency: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_SEARXNG_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "vBot docs",
                        "url": "https://example.com/vbot",
                        "content": "vBot documentation",
                    },
                    {
                        "title": "vBot project",
                        "url": "https://example.com/project",
                        "content": "Project page",
                    },
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 1, "recency": recency},
        lambda key: "",
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    data = assert_success_envelope(result)
    assert data["provider"] == "searxng"
    assert data["query"] == "vbot"
    assert data["count_requested"] == 1
    assert data["result_count"] == 1
    assert data["recency"] == recency
    assert "filters" not in data
    assert data["warnings"] == ["recency enforcement depends on the configured SearXNG engines"]

    request = route.calls[0].request
    assert request.url.params["q"] == "vbot"
    assert request.url.params["format"] == "json"
    assert request.url.params["categories"] == "general"
    assert request.url.params["safesearch"] == "0"
    assert request.url.params["time_range"] == recency

    results = data["results"]
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["title"] == "vBot docs"
    assert results[0]["description"] == "vBot documentation"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_enforces_domain_before_count(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_SEARXNG_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Off-domain first",
                        "url": "https://other.test/vbot",
                        "content": "Must be removed",
                    },
                    {
                        "title": "Matching result",
                        "url": "https://docs.example.com/vbot",
                        "content": "Must remain",
                    },
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "domains": ["example.com"], "count": 1},
        lambda key: "",
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    data = assert_success_envelope(result)
    assert data["applied_domains"] == ["example.com"]
    assert data["result_count"] == 1
    assert data["results"][0]["url"] == "https://docs.example.com/vbot"
    assert data["warnings"] == [
        "domain-filter completeness depends on the configured SearXNG engines; "
        "returned results are still restricted to applied_domains"
    ]
    assert route.calls[0].request.url.params["q"] == "vbot site:example.com"


@pytest.mark.asyncio
async def test_web_search_handler_searxng_rejects_invalid_base_url(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
        lambda: {"provider": "searxng", "searxng": {"base_url": "localhost:8888"}},
    )

    assert_failure_envelope(result, "provider_request_failed")


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

    data = assert_success_envelope(result)
    assert data["count_requested"] == 12
    assert data["page"] == 1
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

    data = assert_success_envelope(result)
    assert data["count_requested"] == 7
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
    assert "default_count" in error["message"]


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

    data = assert_success_envelope(result)
    assert data["page"] == 3
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

    assert_failure_envelope(result, "validation_error")


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
    first, second = data["results"]
    assert first["title"] == "vBot docs"
    assert first["description"] == "The vBot docs & guides"
    assert first["page_age"] == "2026-05-01T00:00:00"
    assert "page_age" not in second


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_page_and_published_date(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_SEARXNG_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "vBot docs",
                        "url": "https://example.com/vbot",
                        "content": "vBot documentation",
                        "publishedDate": "2026-04-30T12:00:00+00:00",
                    }
                ]
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": 2},
        lambda key: "",
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    data = assert_success_envelope(result)
    assert data["page"] == 2
    assert data["results"][0]["page_age"] == "2026-04-30T12:00:00+00:00"
    assert route.calls[0].request.url.params["pageno"] == "2"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_page_warns_about_pagination_gap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_SEARXNG_ENDPOINT).mock(return_value=httpx.Response(200, json={"results": []}))

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "page": 2},
        lambda key: "",
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    data = assert_success_envelope(result)
    warnings = data.get("warnings", [])
    assert any("page size" in w.lower() for w in warnings), (
        f"expected a pagination warning, got {warnings}"
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_page1_has_no_pagination_warning(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_SEARXNG_ENDPOINT).mock(return_value=httpx.Response(200, json={"results": []}))

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        lambda key: "",
        lambda: {
            "provider": "searxng",
            "searxng": {"base_url": "http://localhost:8888"},
        },
    )

    data = assert_success_envelope(result)
    assert "warnings" not in data


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_domain_filter_suppresses_more_results(
    tmp_path: Path,
) -> None:
    """more_results_available must be suppressed with domain filters (B2)."""
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
    assert "more_results_available" not in data
    warnings = data.get("warnings", [])
    assert any("more_results_available" in w for w in warnings), (
        f"expected a domain-paging warning, got {warnings}"
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_without_domains_keeps_more_results(tmp_path: Path) -> None:
    """more_results_available is preserved when no domain filter is applied."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {"results": []},
                "query": {"more_results_available": True},
            },
        )
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data["more_results_available"] is True
    assert "warnings" not in data


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_honors_retry_after_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    observed_hints: list[float | None] = []

    async def _fake_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt
        observed_hints.append(retry_after)

    monkeypatch.setattr("core.tools.web_search.sleep_for_retry", _fake_sleep)

    respx.get(_BRAVE_ENDPOINT).mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={"error": {"message": "rate limited"}},
            ),
            httpx.Response(200, json={"web": {"results": []}}),
        ]
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    assert_success_envelope(result)
    assert observed_hints == [7.0]


def test_api_key_not_in_schema() -> None:
    all_strings = _collect_schema_strings(WEB_SEARCH_TOOL_PARAMETERS)
    for credential_key in (
        "BRAVE_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "SERPER_API_KEY",
        "FIRECRAWL_API_KEY",
    ):
        assert all(credential_key not in value for value in all_strings)


def test_resolve_web_search_settings_logs_unexpected_resolver_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("settings backend exploded")

    with caplog.at_level(logging.ERROR, logger="vbot.tools.web_search"):
        settings, error = _resolve_web_search_settings(boom)

    assert settings is None
    assert error is not None and "could not be loaded" in error
    crash_records = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and "settings resolver crashed unexpectedly" in record.getMessage()
    ]
    assert crash_records, "expected an error log for the crashing settings resolver"
    assert crash_records[0].exc_info is not None


def _read_json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))  # type: ignore[no-any-return]


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
    assert data["query"] == "vbot"
    assert data["count"] == 5
    assert data["page"] == 1
    assert data["result_count"] == 2
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
    assert data["result_count"] == 1
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
    assert data["page"] == 2
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

    monkeypatch.setattr("core.tools.web_search.sleep_for_retry", _fake_sleep)

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
    monkeypatch.setattr(web_search_module, "_MAX_RESPONSE_BYTES", 5)
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
    assert data["result_count"] == 2
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
    assert data["result_count"] == 1
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
    assert data["page"] == 2
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
    assert data["result_count"] == 2
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
    assert data["result_count"] == 1
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
    assert data["result_count"] == 12
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
    assert data["page"] == 2
    assert data["result_count"] == 5
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
    assert data["result_count"] == 3
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
    assert data["result_count"] == 2
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
    assert data["result_count"] == 1
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
    assert data["page"] == 2
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

    monkeypatch.setattr("core.tools.web_search.sleep_for_retry", _fake_sleep)

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
    assert data["result_count"] == 1
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
    assert data["result_count"] == 1
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


_DUCKDUCKGO_HTML = """<html><body>
<div class="result">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fvbot&amp;rut=abc">vBot <b>docs</b></a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fvbot">vBot &amp; documentation for agents</a>
</div>
<div class="result">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide&amp;rut=def">vBot guide</a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide">Getting started</a>
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
    assert data["query"] == "vbot"
    assert data["count"] == 5
    assert data["page"] == 1
    assert data["result_count"] == 3
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
    assert first["content_trust"] == "untrusted_web_content"
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

    respx.get(_DUCKDUCKGO_ENDPOINT).mock(
        return_value=httpx.Response(200, text=_DUCKDUCKGO_HTML)
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "count": 2, "page": 2, "recency": "month"},
        _fake_credential_resolver,
        lambda: {"provider": "duckduckgo"},
    )

    data = assert_success_envelope(result)
    assert data["result_count"] == 1
    results = data["results"]
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["rank"] == 3
    assert results[0]["url"] == "https://example.org/direct"
    assert "recency" not in data
    assert data["warnings"] == [
        web_search_module._DUCKDUCKGO_RECENCY_WARNING,
        web_search_module._DUCKDUCKGO_PAGINATION_WARNING,
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
    assert data["result_count"] == 2


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
