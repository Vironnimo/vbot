"""Ollama: cloud limits behavior."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from core.providers.ollama import (
    OLLAMA_CLOUD_MODE,
    OllamaAdapter,
    OllamaCloudAdapter,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from tests.core.providers.ollama_helpers import (
    OLLAMA_CHAT_URL,
    OLLAMA_CLOUD_CHAT_URL,
    SAMPLE_MESSAGES,
    _last_request_payload,
    _model_lookup,
    _ndjson,
)
from tests.core.providers.ollama_helpers import (
    adapter as adapter,
)


# Output-limit default: Ollama's OpenAI-compatible layer truncates at an
# internal num_predict of 128 when no max_tokens reaches the wire.
class TestCloudOutputLimitDefault:
    """The provider default max_tokens must reach every Cloud payload."""

    @staticmethod
    def _cloud_adapter_with_default() -> OllamaCloudAdapter:
        config = ProviderConfig(
            id="ollama-cloud",
            name="Ollama Cloud",
            adapter="ollama_cloud",
            base_url="https://ollama.com",
            models_endpoint="/api/tags",
            defaults={"max_tokens": 65536},
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API key",
                    auth=AuthConfig(
                        header="Authorization", prefix="Bearer ", credential_key="OLLAMA_API_KEY"
                    ),
                    mode=OLLAMA_CLOUD_MODE,
                    catalog_requires_credentials=False,
                )
            ],
        )
        return OllamaCloudAdapter(
            config,
            "ollama-secret",
            model_lookup=_model_lookup,
            connection_mode=OLLAMA_CLOUD_MODE,
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_payload_sends_default_max_tokens_without_catalog_ceiling(self) -> None:
        """A model with no catalog ceiling still gets a positive max_tokens."""
        # Arrange — plain-model has max_output_tokens None and a 32768 window.
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "Hi"}}]}
            )
        )
        adapter = self._cloud_adapter_with_default()

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="plain-model")

        # Assert — without the default, no max_tokens would be sent and the
        # Cloud compat layer truncates after ~128 tokens.
        payload = _last_request_payload(route)
        max_tokens = payload.get("max_tokens")
        assert isinstance(max_tokens, int) and 0 < max_tokens <= 65536

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_caller_max_tokens_wins_over_default(self) -> None:
        # Arrange
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "Hi"}}]}
            )
        )
        adapter = self._cloud_adapter_with_default()

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="plain-model", max_tokens=512)

        # Assert — the caller allowance survives, context-clamped at most.
        payload = _last_request_payload(route)
        assert isinstance(payload.get("max_tokens"), int)
        assert payload["max_tokens"] <= 512

    def test_bundled_provider_json_ships_the_output_default(self) -> None:
        """The shipped ollama-cloud.json pins the anti-truncation default."""
        # Arrange
        bundled_path = (
            Path(__file__).resolve().parents[3] / "resources" / "providers" / "ollama-cloud.json"
        )

        # Act
        bundled = json.loads(bundled_path.read_text(encoding="utf-8"))

        # Assert
        assert isinstance(bundled.get("defaults"), dict)
        assert isinstance(bundled["defaults"].get("max_tokens"), int)
        assert bundled["defaults"]["max_tokens"] > 0


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
async def test_native_stream_idless_calls_survive_chat_accumulation(
    adapter: OllamaAdapter, batched: bool
) -> None:
    from core.chat.streaming import StreamingAccumulator

    calls = [
        {"function": {"name": "read", "arguments": {"path": "a"}}},
        {"function": {"name": "search", "arguments": {"q": "b"}}},
        {"id": "real_call", "function": {"name": "read", "arguments": {"path": "c"}}},
    ]
    chunks = [
        {"message": {"tool_calls": group}, "done": False}
        for group in ([calls] if batched else [[call] for call in calls])
    ]
    chunks.append({"message": {}, "done": True, "done_reason": "stop"})
    respx.post(OLLAMA_CHAT_URL).mock(return_value=httpx.Response(200, text=_ndjson(*chunks)))
    accumulator = StreamingAccumulator()
    async for delta in adapter.stream(SAMPLE_MESSAGES, model_id="ministral-3:8b"):
        accumulator.add_delta(delta)
    fields = accumulator.finalize_assistant_fields()
    normalized = fields.tool_calls
    assert normalized is not None
    assert len({call["id"] for call in normalized}) == 3
    assert [(call["name"], call["arguments"]) for call in normalized] == [
        ("read", {"path": "a"}),
        ("search", {"q": "b"}),
        ("read", {"path": "c"}),
    ]
    assert normalized[-1]["id"] == "real_call"
    assert fields.finish_reason == "tool_calls"
