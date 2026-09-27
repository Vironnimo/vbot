"""A registered web_search Tool, provider endpoints and result readers for web search tests.

Provider requests are answered with respx routes, so no test reaches the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope
from core.tools.web_search import WEB_SEARCH_TOOL_NAME, register_web_search_tool

API_KEY = "test-api-key"

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html"
EXA_ENDPOINT = "https://api.exa.ai/search"
FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/search"
PERPLEXITY_ENDPOINT = "https://api.perplexity.ai/search"
SERPER_ENDPOINT = "https://google.serper.dev/search"
SEARXNG_ENDPOINT = "http://localhost:8888/search"
TAVILY_ENDPOINT = "https://api.tavily.com/search"

SEARXNG_SETTINGS = {"provider": "searxng", "searxng": {"base_url": "http://localhost:8888"}}


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=WEB_SEARCH_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path / "workspace",
        vbot_root=tmp_path,
        data_root=tmp_path / "data",
    )


def web_search_registry(
    settings: Any = None,
    *,
    credentials: Callable[[str], str] = lambda _key: API_KEY,
) -> ToolRegistry:
    """Return a registry holding web_search with these Settings (None: the defaults)."""
    registry = ToolRegistry()
    register_web_search_tool(registry, credentials, None if settings is None else lambda: settings)
    return registry


async def search(
    tmp_path: Path,
    arguments: Any,
    *,
    provider: str | None = None,
    settings: Any = None,
    credentials: Callable[[str], str] = lambda _key: API_KEY,
) -> dict[str, Any]:
    """Dispatch one web_search call; ``provider`` selects it in otherwise default Settings."""
    if provider is not None:
        settings = SEARXNG_SETTINGS if provider == "searxng" else {"provider": provider}
    registry = web_search_registry(settings, credentials=credentials)
    return await registry.dispatch(make_context(tmp_path), arguments)


@pytest.fixture
def retry_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, float | None]]:
    """Skip retry backoff and record each wait's attempt and Retry-After hint."""
    sleeps: list[tuple[int, float | None]] = []

    async def record(attempt: int, retry_after: float | None = None) -> None:
        sleeps.append((attempt, retry_after))

    monkeypatch.setattr("core.tools._web_search_transport.sleep_for_retry", record)
    return sleeps


def request_json(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))  # type: ignore[no-any-return]


def assert_success_envelope(result: dict[str, Any]) -> dict[str, Any]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
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


def result_blocks(data: dict[str, Any]) -> list[list[str]]:
    """Split a web_search result's numbered content into the lines of each result."""
    content = data["content"]
    assert isinstance(content, str)
    if content.startswith("No results found."):
        return []
    return [block.split("\n") for block in content.split("\n\n")]


def result_urls(data: dict[str, Any]) -> list[str]:
    """Return the URL line of each numbered result."""
    return [lines[1] for lines in result_blocks(data)]
