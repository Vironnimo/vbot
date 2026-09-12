"""Shared fixtures and fakes for web search behavior tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from core.tools.tools import ToolContext, is_tool_result_envelope
from core.tools.web_search import (
    WEB_SEARCH_TOOL_NAME,
)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

_DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html"

_EXA_ENDPOINT = "https://api.exa.ai/search"

_FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/search"

_PERPLEXITY_ENDPOINT = "https://api.perplexity.ai/search"

_SERPER_ENDPOINT = "https://google.serper.dev/search"

_SEARXNG_ENDPOINT = "http://localhost:8888/search"

_TAVILY_ENDPOINT = "https://api.tavily.com/search"


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


def _read_json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))  # type: ignore[no-any-return]
