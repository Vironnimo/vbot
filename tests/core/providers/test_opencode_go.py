"""Tests for OpenCodeGoAdapter."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-opencode-go-key"
OPENCODE_GO_URL = "https://opencode-go.example/v1/chat/completions"
OPENCODE_GO_MESSAGES_URL = "https://opencode-go.example/v1/messages"
ANTHROPIC_MESSAGES_MODELS: tuple[str, ...] = (
    "minimax-m2.7",
    "minimax-m2.5",
    "qwen3.6-plus",
    "qwen3.5-plus",
)


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


def model_with_output_limit(model_id: str, max_output_tokens: int) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=1_000_000,
        max_output_tokens=max_output_tokens,
    )


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

    def test_build_payload_replays_reasoning_for_all_assistants_on_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [
                {"role": "user", "content": "First question"},
                {
                    "role": "assistant",
                    "content": "First answer",
                    "reasoning": "first reasoning",
                    "reasoning_meta": {"reasoning_details": [{"trace": "first"}]},
                    "tool_calls": None,
                },
                {"role": "user", "content": "Second question"},
                {
                    "role": "assistant",
                    "content": "Second answer",
                    "reasoning": "second reasoning",
                    "reasoning_meta": {"reasoning_details": [{"trace": "second"}]},
                    "tool_calls": None,
                },
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assistant_messages = [
            message for message in payload["messages"] if message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        assert assistant_messages[0]["reasoning_content"] == "first reasoning"
        assert assistant_messages[0]["reasoning_details"] == [{"trace": "first"}]
        assert assistant_messages[1]["reasoning_content"] == "second reasoning"
        assert assistant_messages[1]["reasoning_details"] == [{"trace": "second"}]

    def test_build_payload_uses_catalog_output_limit_over_provider_default(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        config = replace(opencode_go_config, defaults={"max_tokens": 4096})
        catalog_model = model_with_output_limit("deepseek-v4-flash", 384_000)
        adapter = OpenCodeGoAdapter(
            config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "Write a complete HTML app."}],
            model_id="deepseek-v4-flash",
        )

        assert payload["max_tokens"] == 384_000

    def test_build_payload_uses_catalog_output_limit_for_vendor_prefixed_model_id(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        config = replace(opencode_go_config, defaults={"max_tokens": 4096})
        catalog_model = model_with_output_limit("deepseek-v4-flash", 384_000)
        adapter = OpenCodeGoAdapter(
            config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "Write a complete HTML app."}],
            model_id="deepseek/deepseek-v4-flash",
        )

        assert payload["max_tokens"] == 384_000

    def test_build_payload_preserves_explicit_output_limit(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        config = replace(opencode_go_config, defaults={"max_tokens": 4096})
        catalog_model = model_with_output_limit("deepseek-v4-flash", 384_000)
        adapter = OpenCodeGoAdapter(
            config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "Write a short file."}],
            model_id="deepseek-v4-flash",
            max_tokens=2048,
        )

        assert payload["max_tokens"] == 2048


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

    @pytest.mark.parametrize("model_id", ANTHROPIC_MESSAGES_MODELS)
    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_model_send_uses_anthropic_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
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
            model_id=model_id,
        )

        assert messages_route.called
        assert not chat_route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_replays_reasoning_meta_only_for_active_continuation_assistant(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        await opencode_go_adapter.send(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
                        ]
                    },
                    "tool_calls": [{"id": "call_old", "name": "old_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "old_tool",
                    "content": json.dumps({"ok": True}),
                },
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "reasoning": "latest thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "latest thinking",
                                "signature": "sig-latest",
                            }
                        ]
                    },
                    "tool_calls": [{"id": "call_latest", "name": "latest_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_latest",
                    "name": "latest_tool",
                    "content": json.dumps({"ok": True}),
                },
            ],
            model_id="minimax-m2.7",
        )

        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        older_blocks = assistant_messages[0].get("content", [])
        latest_blocks = assistant_messages[1].get("content", [])
        assert isinstance(older_blocks, list)
        assert isinstance(latest_blocks, list)
        assert not any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in older_blocks
        )
        assert any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in latest_blocks
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_drops_stale_reasoning_when_latest_assistant_has_no_reasoning(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        await opencode_go_adapter.send(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
                        ]
                    },
                    "tool_calls": [{"id": "call_old", "name": "old_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "old_tool",
                    "content": json.dumps({"ok": True}),
                },
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "tool_calls": [{"id": "call_latest", "name": "latest_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_latest",
                    "name": "latest_tool",
                    "content": json.dumps({"ok": True}),
                },
            ],
            model_id="minimax-m2.7",
        )

        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        older_blocks = assistant_messages[0].get("content", [])
        latest_blocks = assistant_messages[1].get("content", [])
        assert isinstance(older_blocks, list)
        assert isinstance(latest_blocks, list)
        assert not any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in older_blocks
        )
        assert not any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in latest_blocks
        )
        assert any(
            isinstance(block, dict) and block.get("type") == "tool_use" for block in latest_blocks
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_keeps_reasoning_for_tool_continuation_with_synthetic_user_note(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        await opencode_go_adapter.send(
            [
                {"role": "user", "content": "Run tool"},
                {
                    "role": "assistant",
                    "content": "Tool turn assistant",
                    "reasoning": "active thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "active thinking",
                                "signature": "sig-active",
                            }
                        ]
                    },
                    "tool_calls": [{"id": "call_1", "name": "record_note", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "record_note",
                    "content": json.dumps({"ok": True}),
                },
                {
                    "role": "user",
                    "content": (
                        "<system-reminder>\nTool finished background work\n</system-reminder>"
                    ),
                },
            ],
            model_id="minimax-m2.7",
        )

        payload_messages = captured_payload.get("messages", [])
        assert isinstance(payload_messages, list)
        assert payload_messages[-1]["role"] == "user"
        reminder_content = payload_messages[-1].get("content", [])
        assert isinstance(reminder_content, list)
        assert any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text")
            == ("<system-reminder>\nTool finished background work\n</system-reminder>")
            for block in reminder_content
        )

        assistant_messages = [
            message
            for message in payload_messages
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 1
        assistant_blocks = assistant_messages[0].get("content", [])
        assert isinstance(assistant_blocks, list)
        assert any(
            isinstance(block, dict) and block.get("type") == "thinking"
            for block in assistant_blocks
        )
        assert any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in assistant_blocks
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_drops_stale_reasoning_when_completed_tool_span_is_not_tail(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        await opencode_go_adapter.send(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
                        ]
                    },
                    "tool_calls": [{"id": "call_old", "name": "old_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "old_tool",
                    "content": json.dumps({"ok": True}),
                },
                {"role": "user", "content": "Follow-up question after tool turn"},
            ],
            model_id="minimax-m2.7",
        )

        payload_messages = captured_payload.get("messages", [])
        assert isinstance(payload_messages, list)
        assert payload_messages[-1]["role"] == "user"

        assistant_messages = [
            message
            for message in payload_messages
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 1
        assistant_blocks = assistant_messages[0].get("content", [])
        assert isinstance(assistant_blocks, list)
        assert not any(
            isinstance(block, dict) and block.get("type") == "thinking"
            for block in assistant_blocks
        )
        assert any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in assistant_blocks
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_minimax_send_uses_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_chat_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
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

        chat_route = respx.post(OPENCODE_GO_URL).mock(side_effect=_capture_chat_request)
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
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "reasoning_details": [{"trace": "old"}],
                    },
                    "tool_calls": None,
                },
                {"role": "user", "content": "Second"},
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "reasoning": "latest thinking",
                    "reasoning_meta": {
                        "reasoning_details": [{"trace": "latest"}],
                    },
                    "tool_calls": None,
                },
                {"role": "user", "content": "Continue"},
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assert chat_route.called
        assert not messages_route.called
        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        assert assistant_messages[0]["reasoning_content"] == "old thinking"
        assert assistant_messages[0]["reasoning_details"] == [{"trace": "old"}]
        assert assistant_messages[1]["reasoning_content"] == "latest thinking"
        assert assistant_messages[1]["reasoning_details"] == [{"trace": "latest"}]

    @pytest.mark.parametrize("model_id", ANTHROPIC_MESSAGES_MODELS)
    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_model_stream_uses_anthropic_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
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
            model_id=model_id,
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

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_stream_replays_reasoning_meta_only_for_active_continuation_assistant(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                text='event: message_stop\ndata: {"type":"message_stop"}\n\n',
                headers={"content-type": "text/event-stream"},
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        chunks: list[dict[str, str]] = []
        async for chunk in opencode_go_adapter.stream(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
                        ]
                    },
                    "tool_calls": [{"id": "call_old", "name": "old_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "old_tool",
                    "content": json.dumps({"ok": True}),
                },
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "reasoning": "latest thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "latest thinking",
                                "signature": "sig-latest",
                            }
                        ]
                    },
                    "tool_calls": [{"id": "call_latest", "name": "latest_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_latest",
                    "name": "latest_tool",
                    "content": json.dumps({"ok": True}),
                },
            ],
            model_id="minimax-m2.7",
        ):
            chunks.append(chunk)

        assert chunks == []
        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        older_blocks = assistant_messages[0].get("content", [])
        latest_blocks = assistant_messages[1].get("content", [])
        assert isinstance(older_blocks, list)
        assert isinstance(latest_blocks, list)
        assert not any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in older_blocks
        )
        assert any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in latest_blocks
        )

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
