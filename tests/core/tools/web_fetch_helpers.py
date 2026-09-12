"""Shared fixtures and fakes for web fetch behavior tests."""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest

import core.tools.web_fetch as web_fetch_module
from core.tools.tools import ToolContext, is_tool_result_envelope
from core.tools.web_fetch import (
    WEB_FETCH_TOOL_NAME,
    _FetchResult,
    make_web_fetch_handler,
)

# The handler is created by ``make_web_fetch_handler``; this shim builds it with an
# optional fake store and invokes it, so existing ``await web_fetch_handler(ctx, args)``
# call sites stay unchanged while image tests pass an ``attachment_store``.
_FetchHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


def web_fetch_handler(
    context: ToolContext, arguments: dict[str, Any], *, attachment_store: Any = None
) -> Awaitable[dict[str, Any]]:
    handler = cast(_FetchHandler, make_web_fetch_handler(attachment_store))
    return handler(context, arguments)


def make_context(workspace: Path, tool_name: str = WEB_FETCH_TOOL_NAME) -> ToolContext:
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


def web_fetch_arguments(url: str, output: str | None = None) -> dict[str, Any]:
    arguments: dict[str, Any] = {"url": url}
    if output is not None:
        arguments["output"] = output
    return arguments


def make_result(
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    text: str = "",
    url: str = "https://example.com/",
    content: bytes | None = None,
) -> _FetchResult:
    """Build a normalized fetch result with lower-cased header keys.

    ``content`` defaults to the UTF-8 encoding of ``text`` so a text response
    sniffs as text; image/binary tests pass raw bytes explicitly.
    """
    normalized = {name.lower(): value for name, value in (headers or {}).items()}
    body = text.encode("utf-8") if content is None else content
    return _FetchResult(
        status_code=status_code, headers=normalized, text=text, url=url, content=body
    )


class _StreamingResponse:
    """Small curl-response stand-in for testing bounded response collection."""

    def __init__(self, chunks: list[bytes], *, headers: dict[str, str] | None = None) -> None:
        self._chunks = chunks
        self.headers = headers or {}
        self.status_code = 200
        self.url = "https://example.com/stream"
        self.content = b""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    async def aiter_content(self):
        for chunk in self._chunks:
            yield chunk


class _StreamingRequest:
    def __init__(self, response: _StreamingResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _StreamingResponse:
        return self._response

    async def __aexit__(self, *arguments: object) -> None:
        del arguments


class _StreamingSession:
    def __init__(self, response: _StreamingResponse | None = None) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.curl_options: dict[object, object] = {}

    async def __aenter__(self) -> _StreamingSession:
        return self

    async def __aexit__(self, *arguments: object) -> None:
        del arguments

    def stream(self, method: str, url: str, **kwargs: object) -> _StreamingRequest:
        if self._response is None:
            raise AssertionError("stream() requires a configured fake response")
        self.calls.append((method, url, kwargs))
        return _StreamingRequest(self._response)


@pytest.fixture(autouse=True)
def stub_http_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep mocked HTTP tests from creating curl's Windows selector thread."""
    monkeypatch.setattr(
        web_fetch_module,
        "AsyncSession",
        lambda **_kwargs: _StreamingSession(),
    )


def install_http_get(
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[str], _FetchResult],
) -> None:
    """Replace the network seam so no real request is made.

    *responder* maps a requested URL to a canned result; it may raise to simulate
    a transport error.
    """

    async def _fake_http_get(session: object, url: str) -> _FetchResult:
        del session
        return responder(url)

    monkeypatch.setattr(web_fetch_module, "_http_get", _fake_http_get)


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
