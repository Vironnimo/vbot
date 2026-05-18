"""Tests for OpenCodeGoAdapter."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-opencode-go-key"
OPENCODE_GO_URL = "https://opencode-go.example/v1/chat/completions"


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
