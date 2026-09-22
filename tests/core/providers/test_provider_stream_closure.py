"""Public Provider stream closure owns every nested transport iterator."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.lmstudio import LMStudioAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from tests.core.providers.github_copilot_test_support import _copilot_metadata_lookup


class _PartialResponseStream(httpx.AsyncByteStream):
    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for event in self.events:
            yield f"data: {json.dumps(event)}\n\n".encode()

    async def aclose(self) -> None:
        self.closed = True


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model_id", "wire"),
    [
        ("github-copilot", "gemini-3.1-pro-preview", "chat"),
        ("github-copilot", "gpt-5-mini", "responses"),
        ("github-copilot", "claude-sonnet-4.6", "messages"),
        ("openrouter", "test-model", "chat"),
        ("openrouter", "openai/gpt-5.6-luna", "responses"),
        ("lmstudio", "test-model", "chat"),
    ],
)
async def test_public_stream_close_closes_partial_http_response(
    provider: str, model_id: str, wire: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ProviderConfig(
        id=provider,
        name=provider,
        adapter=provider.replace("-", "_"),
        base_url="https://provider.test",
        connections=[
            ConnectionConfig(
                id="test",
                type="api_key",
                label="Test",
                auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key="TEST"),
            )
        ],
    )
    adapter_class = {
        "github-copilot": GitHubCopilotAdapter,
        "openrouter": OpenRouterAdapter,
        "lmstudio": LMStudioAdapter,
    }[provider]
    adapter = adapter_class(config, "test-token", model_lookup=_copilot_metadata_lookup)
    if isinstance(adapter, LMStudioAdapter):
        monkeypatch.setattr(adapter, "_ensure_model_loaded", AsyncMock())
    events: list[dict[str, Any]]
    if wire == "responses":
        events = [{"type": "response.output_text.delta", "delta": "partial"}]
    elif wire == "messages":
        events = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "partial"},
            },
        ]
    else:
        events = [{"choices": [{"index": 0, "delta": {"content": "partial"}}]}]
    body = _PartialResponseStream(events)
    response = httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})
    route = respx.post(url__regex=r"https://provider\.test/.*").mock(return_value=response)
    stream = adapter.stream([{"role": "user", "content": "test"}], model_id=model_id)
    try:
        assert await anext(stream) == {"type": "content_delta", "text": "partial"}
        await stream.aclose()
        assert body.closed
        assert route.call_count == 1
    finally:
        await stream.aclose()
        await adapter.aclose()
