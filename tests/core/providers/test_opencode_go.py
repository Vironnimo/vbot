"""Tests for OpenCodeGoAdapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-opencode-go-key"
OPENCODE_GO_URL = "https://opencode-go.example/v1/chat/completions"
OPENCODE_GO_MESSAGES_URL = "https://opencode-go.example/v1/messages"


def test_public_package_exports_opencode_go_adapter() -> None:
    from core.providers import OpenCodeGoAdapter as PublicOpenCodeGoAdapter

    assert PublicOpenCodeGoAdapter is OpenCodeGoAdapter


@pytest.fixture()
def opencode_go_config() -> ProviderConfig:
    return ProviderConfig(
        id="opencode-go",
        name="OpenCode Go",
        adapter="opencode_go",
        base_url="https://opencode-go.example/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENCODE_GO_API_KEY",
                ),
            )
        ],
    )


@pytest.fixture()
def opencode_go_adapter(opencode_go_config: ProviderConfig) -> OpenCodeGoAdapter:
    return OpenCodeGoAdapter(opencode_go_config, API_KEY)


class TestOpenCodeGoAdapter:
    def test_format_assistant_message_adds_reasoning_content(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        internal_message = {
            "role": "assistant",
            "content": "Answer",
            "reasoning": "I think...",
            "tool_calls": None,
            "reasoning_meta": None,
        }

        wire = opencode_go_adapter._format_assistant_message(internal_message)

        assert wire["reasoning_content"] == "I think..."
        assert wire["content"] == "Answer"
        assert "reasoning" not in wire

    def test_format_assistant_message_skips_reasoning_content_when_reasoning_is_none(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        internal_message = {
            "role": "assistant",
            "content": "Hi",
            "reasoning": None,
            "tool_calls": None,
            "reasoning_meta": None,
        }

        wire = opencode_go_adapter._format_assistant_message(internal_message)

        assert "reasoning_content" not in wire

    def test_format_assistant_message_skips_reasoning_content_when_reasoning_is_empty(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        internal_message = {
            "role": "assistant",
            "content": "Hi",
            "reasoning": "",
            "tool_calls": None,
            "reasoning_meta": None,
        }

        wire = opencode_go_adapter._format_assistant_message(internal_message)

        assert "reasoning_content" not in wire

    @respx.mock
    @pytest.mark.asyncio
    async def test_round_trip_tool_loop_payload_includes_reasoning_content(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        reasoning_text = "Need to call tool first"
        route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "reasoning_content": reasoning_text,
                                "tool_calls": [
                                    {
                                        "id": "call_weather",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": '{"city":"Berlin"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        )

        first_response = await opencode_go_adapter.send(
            [{"role": "user", "content": "Weather in Berlin?"}],
            model_id="deepseek/deepseek-v4-flash",
        )
        normalized_assistant = opencode_go_adapter.normalize_response(first_response)

        payload = opencode_go_adapter._build_payload(
            [
                {"role": "user", "content": "Weather in Berlin?"},
                normalized_assistant,
                {
                    "role": "tool",
                    "tool_call_id": "call_weather",
                    "name": "get_weather",
                    "content": json.dumps({"temp": 22}),
                },
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assistant_wire = next(msg for msg in payload["messages"] if msg.get("role") == "assistant")
        assert route.called
        assert assistant_wire["reasoning_content"] == reasoning_text

    def test_base_adapter_build_payload_does_not_add_reasoning_content(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        base_adapter = OpenAICompatibleAdapter(opencode_go_config, API_KEY)

        payload = base_adapter._build_payload(
            [
                {
                    "role": "assistant",
                    "content": "Answer",
                    "reasoning": "I think...",
                    "tool_calls": None,
                    "reasoning_meta": None,
                }
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assert "reasoning_content" not in payload["messages"][0]


class TestOpenCodeGoAdapterMinimaxRouting:
    @pytest.mark.asyncio
    async def test_constructor_accepts_runtime_factory_signature(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        runtime_base_url = "https://runtime-opencode-go.example/v1"
        runtime_auth = AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="RUNTIME_OPENCODE_GO_KEY",
        )
        adapter = OpenCodeGoAdapter(opencode_go_config, API_KEY, runtime_base_url, runtime_auth)

        try:
            assert str(adapter._client.base_url).rstrip("/") == runtime_base_url
            assert str(adapter._anthropic._client.base_url).rstrip("/") == runtime_base_url
            assert adapter._anthropic._auth_config.header == "x-api-key"
            assert adapter._anthropic._auth_config.prefix == ""
            assert adapter._anthropic._auth_config.credential_key == runtime_auth.credential_key
        finally:
            await adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_uses_anthropic_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                    "stop_reason": "end_turn",
                },
            )
        )
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "fallback"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="minimax-m2.7",
        )

        assert messages_route.called
        assert not chat_route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_minimax_send_uses_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "unused"}],
                    "stop_reason": "end_turn",
                },
            )
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="deepseek/deepseek-v4-flash",
        )

        assert chat_route.called
        assert not messages_route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_stream_uses_anthropic_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                text='event: message_stop\ndata: {"type":"message_stop"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        )
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                text="data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        )

        chunks: list[dict[str, str]] = []
        async for chunk in opencode_go_adapter.stream(
            [{"role": "user", "content": "hello"}],
            model_id="minimax-m2.7",
        ):
            chunks.append(chunk)

        assert chunks == []
        assert messages_route.called
        assert not chat_route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_minimax_stream_uses_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                text="data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        )
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                text='event: message_stop\ndata: {"type":"message_stop"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        )

        chunks: list[dict[str, str]] = []
        async for chunk in opencode_go_adapter.stream(
            [{"role": "user", "content": "hello"}],
            model_id="deepseek/deepseek-v4-flash",
        ):
            chunks.append(chunk)

        assert chunks == []
        assert chat_route.called
        assert not messages_route.called

    def test_normalize_response_routes_openai_by_choices_key(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        result = opencode_go_adapter.normalize_response(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hi"},
                    }
                ],
                "id": "1",
            }
        )

        assert result["role"] == "assistant"
        assert result["content"] == "hi"

    def test_normalize_response_routes_anthropic_when_no_choices(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        result = opencode_go_adapter.normalize_response(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
            }
        )

        assert result["role"] == "assistant"
        assert result["content"] == "hi"

    @pytest.mark.asyncio
    async def test_aclose_closes_both_clients(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        base_client = AsyncMock()
        anthropic_client = AsyncMock()
        opencode_go_adapter._client = base_client
        opencode_go_adapter._anthropic._client = anthropic_client

        await opencode_go_adapter.aclose()

        base_client.aclose.assert_awaited_once()
        anthropic_client.aclose.assert_awaited_once()
