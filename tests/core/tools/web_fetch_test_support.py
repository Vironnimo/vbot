"""Fake public transport and a registered web_fetch Tool for web fetch tests.

Import the autouse fixtures ``stub_http_session`` and ``stub_dns_resolution`` into
a test module so no test there creates a curl session or resolves a real host.
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

import core.tools._public_http as public_http
from core.tools._public_http import PublicResponse
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope
from core.tools.web_fetch import WEB_FETCH_TOOL_NAME, register_web_fetch_tool

IPV6_HOST = "ipv6.example"
IPV6_ADDRESS = "2606:2800:220:1:248:1893:25c8:1946"


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=WEB_FETCH_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path / "workspace",
        vbot_root=tmp_path,
        data_root=tmp_path / "data",
    )


def web_fetch_registry(*, attachment_store: Any = None, **options: Any) -> ToolRegistry:
    """Return a registry holding web_fetch as production registers it."""
    registry = ToolRegistry()
    register_web_fetch_tool(registry, attachment_store=attachment_store, **options)
    return registry


async def fetch(tmp_path: Path, arguments: Any, **registration: Any) -> dict[str, Any]:
    """Dispatch one web_fetch call through a freshly registered Tool."""
    return await web_fetch_registry(**registration).dispatch(make_context(tmp_path), arguments)


def make_result(
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    text: str = "",
    url: str = "https://example.com/",
    content: bytes | None = None,
) -> PublicResponse:
    """Build a normalized fetch result with lower-cased header keys.

    ``content`` defaults to the UTF-8 encoding of ``text`` so a text response
    sniffs as text; image/binary tests pass raw bytes explicitly.
    """
    normalized = {name.lower(): value for name, value in (headers or {}).items()}
    body = text.encode("utf-8") if content is None else content
    return PublicResponse(
        status_code=status_code, headers=normalized, text=text, url=url, content=body
    )


class StreamingResponse:
    """Curl response stand-in that streams its body in the given chunks."""

    def __init__(self, chunks: list[bytes], *, headers: dict[str, str] | None = None) -> None:
        self._chunks = chunks
        self.headers = headers or {}
        self.status_code = 200
        self.url = "https://example.com/stream"
        self.content = b""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    async def aiter_content(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _StreamingRequest:
    def __init__(self, response: StreamingResponse) -> None:
        self._response = response

    async def __aenter__(self) -> StreamingResponse:
        return self._response

    async def __aexit__(self, *arguments: object) -> None:
        del arguments


class StreamingSession:
    """Curl session stand-in that records requests and curl options."""

    def __init__(self, response: StreamingResponse | None = None) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.curl_options: dict[object, object] = {}

    async def __aenter__(self) -> StreamingSession:
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
    monkeypatch.setattr(public_http, "AsyncSession", lambda **_kwargs: StreamingSession())


@pytest.fixture(autouse=True)
def stub_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to a public address; ``IPV6_HOST`` to a public IPv6 one."""

    async def _fake_resolve_host_addresses(host: str, port: int) -> list[object]:
        del port
        address = IPV6_ADDRESS if host.rstrip(".").lower() == IPV6_HOST else "93.184.216.34"
        return [ipaddress.ip_address(address)]

    monkeypatch.setattr(public_http, "_resolve_host_addresses", _fake_resolve_host_addresses)


def install_http_get(
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[str], PublicResponse],
) -> None:
    """Replace the network seam so no real request is made.

    *responder* maps a requested URL to a canned result; it may raise to simulate
    a transport error.
    """

    async def _fake_http_get(session: object, url: str, max_bytes: int) -> PublicResponse:
        del session, max_bytes
        return responder(url)

    monkeypatch.setattr(public_http, "_http_get", _fake_http_get)


@pytest.fixture
def retry_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float | None]:
    """Skip retry backoff and record each wait's Retry-After hint."""
    hints: list[float | None] = []

    async def record(attempt: int, retry_after: float | None = None) -> None:
        del attempt
        hints.append(retry_after)

    monkeypatch.setattr(public_http, "sleep_for_retry", record)
    return hints


def assert_success_envelope(result: dict[str, Any]) -> dict[str, Any]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    assert "content" in data
    assert "owner" not in data and "page" not in data
    return data


def assert_failure_envelope(result: dict[str, Any], code: str) -> dict[str, Any]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error
