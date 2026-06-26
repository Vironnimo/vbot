"""Tests for the built-in web_fetch tool."""

from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
from typing import cast

import httpcore
import httpx
import pytest
import respx

import core.tools.web_fetch as web_fetch_module
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


@pytest.fixture(autouse=True)
def stub_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_resolve_host_addresses(host: str, port: int) -> list[object]:
        del port
        host_mapping: dict[str, tuple[str, ...]] = {
            "example.com": ("93.184.216.34",),
            "target.example": ("93.184.216.34",),
            "public.example": ("93.184.216.34",),
        }
        resolved = host_mapping.get(host.rstrip(".").lower(), ("93.184.216.34",))
        return [ipaddress.ip_address(address) for address in resolved]

    monkeypatch.setattr(web_fetch_module, "_resolve_host_addresses", _fake_resolve_host_addresses)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/private",
        "http://0x7f000001/private",
        "http://127.1/private",
        "http://example.com@127.0.0.1/private",
    ],
)
async def test_web_fetch_handler_rejects_obfuscated_private_hosts(tmp_path: Path, url: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "validation_error")
    assert "blocked" in error["message"].lower()


@respx.mock
@pytest.mark.asyncio
async def test_web_fetch_handler_rejects_redirect_to_private_host(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    start_url = "https://public.example/start"
    blocked_redirect = "http://127.0.0.1/admin"

    def mock_redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, request=request, headers={"Location": blocked_redirect})

    respx.get(start_url).mock(side_effect=mock_redirect)
    private_route = respx.get(blocked_redirect).mock(
        return_value=httpx.Response(200, text="should not be fetched")
    )

    result = await web_fetch_handler(make_context(workspace), {"url": start_url})

    error = assert_failure_envelope(result, "validation_error")
    assert "blocked" in error["message"].lower()
    assert private_route.called is False


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
async def test_web_fetch_handler_network_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/network-fail"

    def mock_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    respx.get(url).mock(side_effect=mock_connect_error)

    with caplog.at_level(logging.WARNING, logger="vbot.tools.web_fetch"):
        result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    assert "request failed" in error["message"].lower()
    assert any(
        record.levelno == logging.WARNING and "web_fetch request failed" in record.getMessage()
        for record in caplog.records
    )


@respx.mock
@pytest.mark.asyncio
async def test_web_fetch_handler_retries_retryable_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/retry"

    async def no_retry_sleep(attempt: int) -> None:
        del attempt

    monkeypatch.setattr(web_fetch_module, "_sleep_for_retry", no_retry_sleep)

    attempts = 0

    def mock_flaky_response(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, request=request, text="try later")
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            text="retried success",
        )

    respx.get(url).mock(side_effect=mock_flaky_response)

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    assert data["content"] == "retried success"
    assert attempts == 3


@respx.mock
@pytest.mark.asyncio
async def test_web_fetch_handler_exhausted_retryable_status_signals_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/always-busy"

    async def no_retry_sleep(attempt: int) -> None:
        del attempt

    monkeypatch.setattr(web_fetch_module, "_sleep_for_retry", no_retry_sleep)

    calls = 0

    def mock_busy(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request, text="busy")

    respx.get(url).mock(side_effect=mock_busy)

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is True
    assert error["attempts_made"] == web_fetch_module._RETRY_MAX_RETRIES + 1
    # All attempts were spent before the tool gave up.
    assert calls == web_fetch_module._RETRY_MAX_RETRIES + 1


@respx.mock
@pytest.mark.asyncio
async def test_web_fetch_handler_non_retryable_status_signals_not_retryable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/not-found"

    respx.get(url).mock(return_value=httpx.Response(404, text="missing"))

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is False
    assert "attempts_made" not in error


@respx.mock
@pytest.mark.asyncio
async def test_web_fetch_handler_transport_error_signals_retryable_single_attempt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/network-fail"

    def mock_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    respx.get(url).mock(side_effect=mock_connect_error)

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    # web_fetch does not loop on transport errors, so it tried exactly once.
    assert error["retryable"] is True
    assert error["attempts_made"] == 1


@pytest.mark.asyncio
async def test_web_fetch_handler_validation_error_signals_not_retryable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_fetch_handler(make_context(workspace), {"url": "ftp://example.com"})

    error = assert_failure_envelope(result, "validation_error")
    assert error["retryable"] is False


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


@pytest.mark.asyncio
async def test_validate_public_target_returns_resolved_ip() -> None:
    pinned = await web_fetch_module._validate_public_target(httpx.URL("https://example.com/path"))

    assert pinned == "93.184.216.34"


@pytest.mark.asyncio
async def test_validate_public_target_returns_literal_ip() -> None:
    pinned = await web_fetch_module._validate_public_target(httpx.URL("https://93.184.216.34/path"))

    assert pinned == "93.184.216.34"


@pytest.mark.asyncio
async def test_pinned_resolution_backend_substitutes_validated_ip() -> None:
    captured: dict[str, object] = {}

    class _FakeInner:
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: object = None,
        ) -> str:
            captured["host"] = host
            captured["port"] = port
            return "stream"

    backend = web_fetch_module._PinnedResolutionBackend(
        cast(httpcore.AsyncNetworkBackend, _FakeInner()), {"example.com": "93.184.216.34"}
    )

    result = await backend.connect_tcp("example.com", 443)

    assert captured == {"host": "93.184.216.34", "port": 443}
    assert result == "stream"


@pytest.mark.asyncio
async def test_pinned_resolution_backend_passes_through_unpinned_host() -> None:
    captured: dict[str, object] = {}

    class _FakeInner:
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: object = None,
        ) -> str:
            captured["host"] = host
            return "stream"

    backend = web_fetch_module._PinnedResolutionBackend(
        cast(httpcore.AsyncNetworkBackend, _FakeInner()), {}
    )

    await backend.connect_tcp("other.example", 80)

    assert captured["host"] == "other.example"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_with_retry_pins_validated_ip(tmp_path: Path) -> None:
    del tmp_path
    url = "https://example.com/page"
    respx.get(url).mock(return_value=httpx.Response(200, text="ok"))

    pins: dict[str, str] = {}
    async with web_fetch_module._make_client(pins) as client:
        response = await web_fetch_module._fetch_with_retry(client, url, pins)

    assert response.status_code == 200
    assert pins["example.com"] == "93.184.216.34"


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
