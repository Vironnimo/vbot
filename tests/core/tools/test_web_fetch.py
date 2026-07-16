"""Tests for the built-in web_fetch tool."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from curl_cffi import CurlOpt
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

import core.tools.read_extract as read_extract_module
import core.tools.web_fetch as web_fetch_module
from core.attachments import AttachmentTooLargeError
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope
from core.tools.web_fetch import (
    WEB_FETCH_TOOL_DESCRIPTION,
    WEB_FETCH_TOOL_NAME,
    WEB_FETCH_TOOL_PARAMETERS,
    _FetchResult,
    extract_content,
    make_web_fetch_handler,
    register_web_fetch_tool,
)
from core.utils.retry import MAX_RETRIES


@dataclass(frozen=True)
class _FakeRecord:
    id: str
    filename: str
    media_type: str


class _FakeAttachmentStore:
    """Records ``store()`` calls; optionally raises to simulate rejection."""

    def __init__(self, *, error: Exception | None = None, media_type: str = "image/png") -> None:
        self._error = error
        self._media_type = media_type
        self.stored: list[tuple[str, bytes]] = []

    def store(self, filename: str, data: bytes) -> _FakeRecord:
        if self._error is not None:
            raise self._error
        self.stored.append((filename, data))
        return _FakeRecord(id="att-web-1", filename=filename, media_type=self._media_type)


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
        app_root=workspace.parent,
        data_root=workspace.parent / "data",
    )


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
    def __init__(self, response: _StreamingResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def stream(self, method: str, url: str, **kwargs: object) -> _StreamingRequest:
        self.calls.append((method, url, kwargs))
        return _StreamingRequest(self._response)


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


@pytest.mark.asyncio
async def test_http_get_collects_streamed_response_with_existing_text_decoding() -> None:
    session = _StreamingSession(
        _StreamingResponse(
            [b"hello ", b"world"],
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
    )

    result = await web_fetch_module._http_get(cast(Any, session), "https://example.com/stream")

    assert result.content == b"hello world"
    assert result.text == "hello world"
    assert session.calls == [
        (
            "GET",
            "https://example.com/stream",
            {"allow_redirects": False, "timeout": web_fetch_module._REQUEST_TIMEOUT},
        )
    ]


@pytest.mark.asyncio
async def test_http_get_stops_unknown_length_response_at_download_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_fetch_module, "_MAX_RESPONSE_BYTES", 5)
    session = _StreamingSession(_StreamingResponse([b"abc", b"def"]))

    with pytest.raises(web_fetch_module._ResponseTooLargeError, match="download limit"):
        await web_fetch_module._http_get(cast(Any, session), "https://example.com/stream")


@pytest.mark.asyncio
async def test_web_fetch_reports_response_over_download_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def _raise_too_large(_session: object, _url: str) -> _FetchResult:
        raise web_fetch_module._ResponseTooLargeError("response exceeds the 50 MB download limit")

    monkeypatch.setattr(web_fetch_module, "_http_get", _raise_too_large)

    result = await web_fetch_handler(make_context(workspace), {"url": "https://example.com/large"})

    error = assert_failure_envelope(result, "response_too_large")
    assert error["retryable"] is False


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

    register_web_fetch_tool(registry, attachment_store=None)

    tool = registry.get("web_fetch")
    assert tool.name == WEB_FETCH_TOOL_NAME == "web_fetch"
    assert tool.description == WEB_FETCH_TOOL_DESCRIPTION
    # The description tells the agent it can view image URLs (a user requirement).
    assert "image" in WEB_FETCH_TOOL_DESCRIPTION.lower()
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


@pytest.mark.asyncio
async def test_web_fetch_handler_rejects_redirect_to_private_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    start_url = "https://public.example/start"
    blocked_redirect = "http://127.0.0.1/admin"

    fetched: list[str] = []

    def responder(url: str) -> _FetchResult:
        fetched.append(url)
        if url == start_url:
            return make_result(status_code=302, headers={"Location": blocked_redirect})
        return make_result(status_code=200, text="should not be fetched")

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), {"url": start_url})

    error = assert_failure_envelope(result, "validation_error")
    assert "blocked" in error["message"].lower()
    assert blocked_redirect not in fetched


@pytest.mark.asyncio
async def test_web_fetch_handler_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/not-found"

    install_http_get(
        monkeypatch, lambda _url: make_result(status_code=404, text="missing", url=url)
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    assert "404" in error["message"]


@pytest.mark.asyncio
async def test_web_fetch_handler_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/network-fail"

    def responder(_url: str) -> _FetchResult:
        raise CurlConnectionError("connection refused")

    install_http_get(monkeypatch, responder)

    with caplog.at_level(logging.WARNING, logger="vbot.tools.web_fetch"):
        result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    assert "request failed" in error["message"].lower()
    assert any(
        record.levelno == logging.WARNING and "web_fetch request failed" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_web_fetch_handler_retries_retryable_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/retry"

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(web_fetch_module, "sleep_for_retry", no_retry_sleep)

    attempts = 0

    def responder(_url: str) -> _FetchResult:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return make_result(status_code=503, text="try later")
        return make_result(
            status_code=200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            text="retried success",
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    assert data["content"] == "retried success"
    assert attempts == 3


@pytest.mark.asyncio
async def test_web_fetch_handler_exhausted_retryable_status_signals_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/always-busy"

    async def no_retry_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt, retry_after

    monkeypatch.setattr(web_fetch_module, "sleep_for_retry", no_retry_sleep)

    calls = 0

    def responder(_url: str) -> _FetchResult:
        nonlocal calls
        calls += 1
        return make_result(status_code=503, text="busy")

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is True
    assert error["attempts_made"] == MAX_RETRIES + 1
    # All attempts were spent before the tool gave up.
    assert calls == MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_web_fetch_handler_honors_retry_after_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/rate-limited"

    observed_hints: list[float | None] = []

    async def recording_sleep(attempt: int, retry_after: float | None = None) -> None:
        del attempt
        observed_hints.append(retry_after)

    monkeypatch.setattr(web_fetch_module, "sleep_for_retry", recording_sleep)

    attempts = 0

    def responder(_url: str) -> _FetchResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return make_result(
                status_code=429,
                headers={"Retry-After": "7"},
                text="rate limited",
            )
        return make_result(
            status_code=200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            text="recovered",
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    assert data["content"] == "recovered"
    assert observed_hints == [7.0]


@pytest.mark.asyncio
async def test_web_fetch_handler_non_retryable_status_signals_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/not-found"

    install_http_get(monkeypatch, lambda _url: make_result(status_code=404, text="missing"))

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is False
    assert "attempts_made" not in error


@pytest.mark.asyncio
async def test_web_fetch_handler_transport_error_signals_retryable_single_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/network-fail"

    def responder(_url: str) -> _FetchResult:
        raise CurlConnectionError("connection refused")

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    # web_fetch does not loop on transport errors, so it tried exactly once.
    assert error["retryable"] is True
    assert error["attempts_made"] == 1


@pytest.mark.asyncio
async def test_web_fetch_handler_redirect_limit_signals_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/loop"

    # A same-host redirect that never terminates exhausts the hop budget.
    def responder(request_url: str) -> _FetchResult:
        return make_result(
            status_code=302,
            headers={"Location": "https://example.com/loop-next"},
            url=request_url,
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is False
    assert "too many redirects" in error["message"].lower()


@pytest.mark.asyncio
async def test_web_fetch_handler_validation_error_signals_not_retryable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_fetch_handler(make_context(workspace), {"url": "ftp://example.com"})

    error = assert_failure_envelope(result, "validation_error")
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_web_fetch_handler_html_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=html,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Hello" in content
    assert "World" in content
    assert "<h1>" not in content
    assert "<p>" not in content


@pytest.mark.asyncio
async def test_web_fetch_handler_raw_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/raw"
    html = "<html><body><h1>Raw Heading</h1></body></html>"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "text/html"}, text=html, url=url
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url, "raw": True})

    data = assert_success_envelope(result)
    assert data["content"] == html


@pytest.mark.asyncio
async def test_web_fetch_handler_include_links_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "text/html"}, text=html, url=page_url
        ),
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


_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


@pytest.mark.asyncio
async def test_web_fetch_handler_image_url_stores_attachment_and_emits_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/photo.png"
    store = _FakeAttachmentStore(media_type="image/png")

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "image/png"},
            content=_PNG_BYTES,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url}, attachment_store=store)

    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    data = result["data"]
    assert isinstance(data, dict)
    assert "photo.png" in data["content"]
    assert result["artifacts"] == [
        {
            "kind": "read_media",
            "attachment_id": "att-web-1",
            "filename": "photo.png",
            "media_type": "image/png",
        }
    ]
    # The exact fetched bytes were handed to the store under the URL's filename.
    assert store.stored == [("photo.png", _PNG_BYTES)]


@pytest.mark.asyncio
async def test_web_fetch_handler_image_url_shown_even_with_raw_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/photo.png"
    store = _FakeAttachmentStore(media_type="image/png")

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "image/png"}, content=_PNG_BYTES, url=url
        ),
    )

    result = await web_fetch_handler(
        make_context(workspace), {"url": url, "raw": True}, attachment_store=store
    )

    assert result["ok"] is True
    artifacts = result["artifacts"]
    assert isinstance(artifacts, list)
    assert artifacts[0]["kind"] == "read_media"


@pytest.mark.asyncio
async def test_web_fetch_handler_image_attachment_error_maps_to_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/huge.png"
    store = _FakeAttachmentStore(
        error=AttachmentTooLargeError("Attachment size 99 exceeds limit 4")
    )

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "image/png"}, content=_PNG_BYTES, url=url
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url}, attachment_store=store)

    error = assert_failure_envelope(result, "attachment_error")
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_web_fetch_handler_image_without_store_returns_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/photo.png"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "image/png"}, content=_PNG_BYTES, url=url
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "could not be loaded" in content


@pytest.mark.asyncio
async def test_web_fetch_handler_binary_content_returns_notice_not_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/installer.exe"
    exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00garbage\x00bytes"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/octet-stream"},
            content=exe_bytes,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Binary content" in content
    assert "application/octet-stream" in content
    # The decoded binary body is never surfaced as text.
    assert "garbage" not in content


@pytest.mark.asyncio
async def test_web_fetch_handler_binary_detected_by_nul_without_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/data.bin"
    # All bytes are ASCII, so the sniffer decodes it as text/plain; the embedded
    # NUL is what still classifies it as binary.
    blob = b"\x01\x02\x00\x03\x04binary\x00payload"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(status_code=200, headers={}, content=blob, url=url),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Binary content" in content


def _minimal_pdf(lines: list[str]) -> bytes:
    """Build a minimal single-page PDF drawing ``lines`` (empty → no text layer)."""
    operators = b"BT /F1 24 Tf 72 720 Td "
    for line in lines:
        operators += b"(" + line.encode("latin-1") + b") Tj 0 -28 Td "
    operators += b"ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(operators), operators),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % index + body + b"\nendobj\n"
    xref_position = len(pdf)
    pdf += b"xref\n0 %d\n" % (len(objects) + 1)
    pdf += b"0000000000 65535 f \n"
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    pdf += b"startxref\n%d\n%%%%EOF" % xref_position
    return bytes(pdf)


def _minimal_docx(text: str) -> bytes:
    """Build a docx whose ``[Content_Types].xml`` makes it sniff as a Word file."""
    from io import BytesIO
    from zipfile import ZipFile

    content_types = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_web_fetch_extracts_pdf_as_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/report.pdf"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/pdf"},
            content=_minimal_pdf(["Hello PDF"]),
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert f"[Extracted text from {url} (PDF document)]" in content
    assert "# Page 1" in content
    assert "Hello PDF" in content


@pytest.mark.asyncio
async def test_web_fetch_extracts_docx_recognized_by_media_type_without_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # A download URL with no usable extension: the sniffed OOXML type must drive
    # detection, not the URL path.
    url = "https://example.com/download"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/octet-stream"},
            content=_minimal_docx("Web doc body"),
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert f"[Extracted text from {url} (Word document)]" in content
    assert "Web doc body" in content


@pytest.mark.asyncio
async def test_web_fetch_rejects_document_expansion_over_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/large.docx"
    monkeypatch.setattr(read_extract_module, "_MAX_DOCUMENT_EXTRACTED_BYTES", 128)

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={
                "Content-Type": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
            },
            content=_minimal_docx("x" * 512),
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    error = assert_failure_envelope(result, "document_too_large")
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_web_fetch_scanned_pdf_reports_no_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/scan.pdf"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/pdf"},
            content=_minimal_pdf([]),
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "(no extractable text)" in content


@pytest.mark.asyncio
async def test_web_fetch_malformed_pdf_returns_binary_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/broken.pdf"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/pdf"},
            content=b"%PDF-1.4 not really a pdf \x00 body",
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Binary content" in content
    assert "application/pdf" in content


@pytest.mark.asyncio
async def test_web_fetch_handler_non_html_json_returns_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/api/data"
    body = '{"recipe": "cake", "tasty": true}'

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/json"},
            text=body,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), {"url": url})

    data = assert_success_envelope(result)
    assert data["content"] == body


@pytest.mark.asyncio
async def test_validate_public_target_returns_resolved_ip() -> None:
    host, pinned = await web_fetch_module._validate_public_target("https", "example.com", 443)

    assert host == "example.com"
    assert pinned == "93.184.216.34"


@pytest.mark.asyncio
async def test_validate_public_target_returns_literal_ip() -> None:
    host, pinned = await web_fetch_module._validate_public_target("https", "93.184.216.34", 443)

    assert host == "93.184.216.34"
    assert pinned == "93.184.216.34"


@pytest.mark.asyncio
async def test_fetch_with_retry_pins_validated_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.com/page"

    install_http_get(monkeypatch, lambda _url: make_result(status_code=200, text="ok", url=url))

    resolve_map: dict[tuple[str, int], str] = {}
    async with web_fetch_module._make_session() as session:
        result = await web_fetch_module._fetch_with_retry(session, url, resolve_map)

        # The validated IP is both recorded and handed to curl's RESOLVE map so
        # the connection targets exactly the address that cleared validation.
        assert resolve_map[("example.com", 443)] == "93.184.216.34"
        assert session.curl_options[CurlOpt.RESOLVE] == ["example.com:443:93.184.216.34"]

    assert result.status_code == 200


@pytest.mark.asyncio
async def test_fetch_with_retry_brackets_ipv6_resolve_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/page"

    async def validate_ipv6_target(_scheme: str, host: str | None, _port: int) -> tuple[str, str]:
        assert host == "example.com"
        return "example.com", "2606:2800:220:1:248:1893:25c8:1946"

    monkeypatch.setattr(web_fetch_module, "_validate_public_target", validate_ipv6_target)
    install_http_get(monkeypatch, lambda _url: make_result(status_code=200, text="ok", url=url))

    resolve_map: dict[tuple[str, int], str] = {}
    async with web_fetch_module._make_session() as session:
        await web_fetch_module._fetch_with_retry(session, url, resolve_map)

        assert session.curl_options[CurlOpt.RESOLVE] == [
            "example.com:443:[2606:2800:220:1:248:1893:25c8:1946]"
        ]


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
