"""Tests for the built-in web_search tool."""

from __future__ import annotations

import logging
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

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_SEARXNG_ENDPOINT = "http://localhost:8888/search"


def make_context(workspace: Path, tool_name: str = WEB_SEARCH_TOOL_NAME) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        app_root=workspace.parent,
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
    assert parameters["additionalProperties"] is False

    properties = parameters["properties"]
    assert "provider" not in properties
    assert set(properties) == {"query", "count", "freshness", "date_after", "date_before"}
    count_schema = properties["count"]
    assert count_schema["minimum"] == 1
    assert count_schema["maximum"] == 20


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
async def test_web_search_handler_invalid_date_format(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "date_after": "not-a-date"},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
async def test_web_search_handler_date_range_inverted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {
            "query": "vbot",
            "date_after": "2025-12-31",
            "date_before": "2025-01-01",
        },
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
async def test_web_search_handler_invalid_freshness(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "freshness": "unknown"},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "validation_error")


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

    async def _fake_sleep(attempt: int) -> None:
        sleep_attempts.append(attempt)

    monkeypatch.setattr("core.tools.web_search._sleep_for_retry", _fake_sleep)

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

    async def _fake_sleep(attempt: int) -> None:
        sleep_attempts.append(attempt)

    monkeypatch.setattr("core.tools.web_search._sleep_for_retry", _fake_sleep)

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

    async def _fake_sleep(attempt: int) -> None:
        del attempt

    monkeypatch.setattr("core.tools.web_search._sleep_for_retry", _fake_sleep)

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
    assert error["attempts_made"] == web_search_module._RETRY_MAX_RETRIES + 1
    assert len(route.calls) == web_search_module._RETRY_MAX_RETRIES + 1


@respx.mock
@pytest.mark.asyncio
async def test_web_search_brave_network_error_signals_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def _fake_sleep(attempt: int) -> None:
        del attempt

    monkeypatch.setattr("core.tools.web_search._sleep_for_retry", _fake_sleep)

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
    assert error["attempts_made"] == web_search_module._RETRY_MAX_RETRIES + 1


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
async def test_web_search_handler_brave_success_with_freshness(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"web": {"results": []}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot", "freshness": "week"},
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data["result_count"] == 0
    request = route.calls[0].request
    assert request.url.params["freshness"] == "pw"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_success_with_date_range(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    route = respx.get(_BRAVE_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"web": {"results": []}})
    )

    result = await web_search_handler(
        make_context(workspace),
        {
            "query": "vbot",
            "date_after": "2025-01-01",
            "date_before": "2025-12-31",
        },
        _fake_credential_resolver,
    )

    data = assert_success_envelope(result)
    assert data["result_count"] == 0
    request = route.calls[0].request
    assert request.url.params["freshness"] == "2025-01-01to2025-12-31"


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_searxng_success_without_api_key(tmp_path: Path) -> None:
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
        {"query": "vbot", "count": 1, "freshness": "day"},
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
    assert data["filters"] == {"time_range": "day"}

    request = route.calls[0].request
    assert request.url.params["q"] == "vbot"
    assert request.url.params["format"] == "json"
    assert request.url.params["categories"] == "general"
    assert request.url.params["safesearch"] == "0"
    assert request.url.params["time_range"] == "day"

    results = data["results"]
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["title"] == "vBot docs"
    assert results[0]["description"] == "vBot documentation"


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


def test_api_key_not_in_schema() -> None:
    all_strings = _collect_schema_strings(WEB_SEARCH_TOOL_PARAMETERS)
    assert all("BRAVE_API_KEY" not in value for value in all_strings)


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
