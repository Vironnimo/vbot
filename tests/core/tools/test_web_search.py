"""Tests for the built-in web_search tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope
from core.tools.web_search import (
    WEB_SEARCH_TOOL_DESCRIPTION,
    WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_PARAMETERS,
    register_web_search_tool,
    web_search_handler,
)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


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
async def test_web_search_handler_brave_http_error(tmp_path: Path) -> None:
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

    assert_failure_envelope(result, "provider_request_failed")


@respx.mock
@pytest.mark.asyncio
async def test_web_search_handler_brave_network_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def _raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    respx.get(_BRAVE_ENDPOINT).mock(side_effect=_raise_connect_error)

    result = await web_search_handler(
        make_context(workspace),
        {"query": "vbot"},
        _fake_credential_resolver,
    )

    assert_failure_envelope(result, "provider_request_failed")


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


def test_api_key_not_in_schema() -> None:
    all_strings = _collect_schema_strings(WEB_SEARCH_TOOL_PARAMETERS)
    assert all("BRAVE_API_KEY" not in value for value in all_strings)
