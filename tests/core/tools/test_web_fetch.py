"""Web fetch: validation behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

import core.tools.web_fetch as web_fetch_module
from core.providers.tool_schema import render_tool_definitions
from core.tools.tools import ToolRegistry
from core.tools.web_fetch import (
    WEB_FETCH_TOOL_DESCRIPTION,
    WEB_FETCH_TOOL_NAME,
    WEB_FETCH_TOOL_PARAMETERS,
    _FetchResult,
    register_web_fetch_tool,
)
from tests.core.tools.web_fetch_helpers import (
    _StreamingResponse,
    _StreamingSession,
    assert_failure_envelope,
    install_http_get,
    make_context,
    make_result,
    web_fetch_arguments,
    web_fetch_handler,
)
from tests.core.tools.web_fetch_helpers import (
    stub_dns_resolution as stub_dns_resolution,
)
from tests.core.tools.web_fetch_helpers import (
    stub_http_session as stub_http_session,
)


def test_make_session_requests_browser_impersonation(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor = Mock(return_value=_StreamingSession())
    monkeypatch.setattr(web_fetch_module, "AsyncSession", constructor)

    session = web_fetch_module._make_session()

    assert isinstance(session, _StreamingSession)
    constructor.assert_called_once_with(impersonate=web_fetch_module._IMPERSONATE_TARGET)


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

    with pytest.raises(web_fetch_module._ResponseTooLargeError):
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

    result = await web_fetch_handler(
        make_context(workspace),
        web_fetch_arguments("https://example.com/large"),
    )

    error = assert_failure_envelope(result, "response_too_large")
    assert error["retryable"] is False


def test_register_web_fetch_tool_schema() -> None:
    registry = ToolRegistry()

    register_web_fetch_tool(registry, attachment_store=None)

    tool = registry.get("web_fetch")
    assert tool.name == WEB_FETCH_TOOL_NAME == "web_fetch"
    assert tool.description == WEB_FETCH_TOOL_DESCRIPTION
    assert tool.description
    assert tool.parameters == WEB_FETCH_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["web_fetch"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition["name"] == "web_fetch"
    assert definition["description"] == WEB_FETCH_TOOL_DESCRIPTION

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["url"]
    assert "additionalProperties" not in parameters
    assert set(parameters["properties"]) == {"url", "output"}
    output = parameters["properties"]["output"]
    assert output["type"] == "string"
    assert output["enum"] == ["markdown", "text", "raw"]
    assert isinstance(output["description"], str)
    assert output["description"]


def test_web_fetch_openai_wire_preserves_optional_output_and_disables_strict_mode() -> None:
    [definition] = render_tool_definitions(
        [
            {
                "name": WEB_FETCH_TOOL_NAME,
                "description": WEB_FETCH_TOOL_DESCRIPTION,
                "parameters": WEB_FETCH_TOOL_PARAMETERS,
            }
        ],
        profile="explicit_non_strict",
    )

    parameters = definition["parameters"]
    assert parameters["required"] == ["url"]
    assert definition["strict"] is False
    assert "additionalProperties" not in parameters
    assert parameters["properties"]["output"]["enum"] == ["markdown", "text", "raw"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"url": "https://example.com", "output": "unsupported"},
            "output must be one of",
        ),
        (
            {"url": "https://example.com", "output": "markdown", "raw": False},
            "Unknown argument(s): raw",
        ),
        (
            {
                "url": "https://example.com",
                "output": "markdown",
                "include_links": True,
            },
            "Unknown argument(s): include_links",
        ),
    ],
)
async def test_web_fetch_handler_rejects_missing_invalid_or_legacy_output_arguments(
    tmp_path: Path,
    arguments: dict[str, Any],
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_fetch_handler(make_context(workspace), arguments)

    error = assert_failure_envelope(result, "validation_error")
    assert message in error["message"]
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_web_fetch_handler_rejects_non_http_scheme(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_fetch_handler(
        make_context(workspace),
        web_fetch_arguments("ftp://example.com"),
    )

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

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

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

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

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

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(start_url))

    error = assert_failure_envelope(result, "request_error")
    assert "blocked" in error["message"].lower()
    assert blocked_redirect not in fetched


@pytest.mark.asyncio
async def test_web_fetch_handler_first_hop_blocked_is_validation_error(
    tmp_path: Path,
) -> None:
    """A first-hop private URL must still be validation_error (B5 boundary)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_fetch_handler(
        make_context(workspace),
        web_fetch_arguments("http://127.0.0.1/admin"),
    )

    error = assert_failure_envelope(result, "validation_error")
    assert "blocked" in error["message"].lower()
