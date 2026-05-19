"""Tests for the built-in web_fetch tool."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope
from core.tools.web_fetch import (
    WEB_FETCH_TOOL_DESCRIPTION,
    WEB_FETCH_TOOL_NAME,
    WEB_FETCH_TOOL_PARAMETERS,
    extract_content,
    register_web_fetch_tool,
    web_fetch_handler,
)


def make_context(workspace: Path, tool_name: str = WEB_FETCH_TOOL_NAME) -> ToolContext:
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


def assert_success_envelope(result: dict[str, object]) -> dict[str, object]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    assert set(data) == {"content"}
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


def test_register_web_fetch_tool_schema() -> None:
    registry = ToolRegistry()

    register_web_fetch_tool(registry)

    tool = registry.get("web_fetch")
    assert tool.name == WEB_FETCH_TOOL_NAME == "web_fetch"
    assert tool.description == WEB_FETCH_TOOL_DESCRIPTION
    assert tool.parameters == WEB_FETCH_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["web_fetch"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition["name"] == "web_fetch"
    assert definition["description"] == WEB_FETCH_TOOL_DESCRIPTION

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["url"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"url", "include_links", "raw"}


@pytest.mark.asyncio
async def test_web_fetch_handler_rejects_non_http_scheme(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_fetch_handler(make_context(workspace), {"url": "ftp://example.com"})

    error = assert_failure_envelope(result, "validation_error")
    assert "http/https" in error["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://10.0.0.1/internal",
        "https://localhost/admin",
    ],
)
async def test_web_fetch_handler_rejects_ssrf_prefixes(tmp_path: Path, url: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "validation_error")
    assert "blocked" in error["message"].lower()


@respx.mock
@pytest.mark.asyncio
async def test_web_fetch_handler_http_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/not-found"

    def mock_not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request, text="missing")

    respx.get(url).mock(side_effect=mock_not_found)

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    assert "404" in error["message"]


@respx.mock
@pytest.mark.asyncio
async def test_web_fetch_handler_network_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/network-fail"

    def mock_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    respx.get(url).mock(side_effect=mock_connect_error)

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    assert "request failed" in error["message"].lower()


@respx.mock
@pytest.mark.asyncio
async def test_web_fetch_handler_html_extraction(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/page"
    html = """
    <html>
      <head><title>Example Title</title></head>
      <body>
        <h1>Hello</h1>
        <p>World <a href="/docs">Docs</a></p>
      </body>
    </html>
    """

    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=html,
        )
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Hello" in content
    assert "World" in content
    assert "<h1>" not in content
    assert "<p>" not in content


@respx.mock
@pytest.mark.asyncio
async def test_web_fetch_handler_raw_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/raw"
    html = "<html><body><h1>Raw Heading</h1></body></html>"

    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=html,
        )
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url, "raw": True})

    data = assert_success_envelope(result)
    assert data["content"] == html


@respx.mock
@pytest.mark.asyncio
async def test_web_fetch_handler_include_links_false(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    page_url = "https://example.com/links"
    link_url = "https://target.example/resource"
    html = f"""
    <html>
      <body>
        <p>Read <a href="{link_url}">Visible Link</a> now.</p>
      </body>
    </html>
    """

    respx.get(page_url).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=html,
        )
    )

    result = await web_fetch_handler(
        make_context(workspace),
        {"url": page_url, "include_links": False},
    )

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Visible Link" in content
    assert link_url not in content


def test_extract_content_strips_scripts_and_styles() -> None:
    html = """
    <html>
      <head>
        <title>Metadata Title</title>
        <style>body { display: none; }</style>
      </head>
      <body>
        <script>console.log('hide me')</script>
        <p>Visible Text</p>
      </body>
    </html>
    """

    text, metadata = extract_content(html, "https://example.com")

    assert "Visible Text" in text
    assert "console.log" not in text
    assert "display: none" not in text
    assert metadata["title"] == "Metadata Title"
