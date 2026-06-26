"""Tests for OpenCodeGoAdapter."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import core.providers.opencode_go as opencode_go_module
from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-opencode-go-key"
OPENCODE_GO_URL = "https://opencode-go.example/v1/chat/completions"
OPENCODE_GO_MESSAGES_URL = "https://opencode-go.example/v1/messages"
# Per-model wire protocol is now DATA (metadata.opencode_go.protocol), not a
# hardcoded adapter set. These ids carry "anthropic" in the protocol map below
# and must route through the internal Messages adapter.
ANTHROPIC_MESSAGES_MODELS: tuple[str, ...] = (
    "minimax-m2.7",
    "minimax-m2.5",
    "qwen3.7-plus",
)
# A small per-model protocol map mirroring what the opencode-go override carries
# on ``metadata.opencode_go.protocol`` (the published table). Models not listed
# here are "unknown" to the adapter and route to the safe OpenAI default.
_PROTOCOL_BY_MODEL: dict[str, str] = {
    "minimax-m2.7": "anthropic",
    "minimax-m2.5": "anthropic",
    "minimax-m3": "anthropic",
    "qwen3.7-plus": "anthropic",
    "qwen3.7-max": "anthropic",
    "qwen3.6-plus": "anthropic",
    "deepseek-v4-flash": "openai",
    "deepseek-v4-pro": "openai",
    "qwen3.6-plus-openai": "openai",
}


def _model_with_protocol(model_id: str, protocol: str | None) -> Model:
    metadata: dict[str, object] = {}
    if protocol is not None:
        metadata = {"opencode_go": {"protocol": protocol}}
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
        max_output_tokens=131_072,
        metadata=metadata,
    )


def _protocol_lookup(model_id: str) -> Model | None:
    """Resolve the metadata-carrying model for one bare or vendor-prefixed id."""

    bare = model_id.split("::", 1)[0]
    candidates = [model_id, bare]
    if "/" in bare:
        candidates.append(bare.rsplit("/", 1)[-1])
    for candidate in candidates:
        if candidate in _PROTOCOL_BY_MODEL:
            return _model_with_protocol(candidate, _PROTOCOL_BY_MODEL[candidate])
    return None


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
    # The adapter routes on ``metadata.opencode_go.protocol`` resolved via
    # ``model_lookup``; inject the protocol map so routing is data-driven.
    return OpenCodeGoAdapter(opencode_go_config, API_KEY, model_lookup=_protocol_lookup)


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
    @pytest.mark.parametrize(
        "model_id",
        [*ANTHROPIC_MESSAGES_MODELS, "deepseek-v4-flash", "deepseek/deepseek-v4-flash"],
    )
    def test_reasoning_replay_policy_is_full_history_on_both_routes(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        assert opencode_go_adapter.reasoning_replay_policy(model_id) == "full_history"

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

    def test_deepseek_none_thinking_effort_omits_reasoning_effort(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Reply OK."}],
            model_id="deepseek-v4-flash",
            thinking_effort="none",
        )

        assert "reasoning_effort" not in payload

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
    async def test_openai_marked_model_send_uses_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "messages"}],
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
                            "message": {"role": "assistant", "content": "chat"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="deepseek-v4-flash",
        )

        assert chat_route.called
        assert not messages_route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_model_defaults_to_openai_path_and_warns(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A model with no protocol metadata routes the safe OpenAI default + logs a warn."""

        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "messages"}],
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
                            "message": {"role": "assistant", "content": "chat"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )

        opencode_go_module._warned_unmarked_models.clear()
        with caplog.at_level("WARNING", logger="vbot.providers.opencode_go"):
            await opencode_go_adapter.send(
                [{"role": "user", "content": "hello"}],
                model_id="brand-new-unlisted-model",
            )

        assert chat_route.called
        assert not messages_route.called
        assert any(
            "no metadata protocol" in record.getMessage()
            and "brand-new-unlisted-model" in record.getMessage()
            for record in caplog.records
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_model_warns_once_per_process(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unmarked model logs its routing warning once, not on every request."""

        respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "chat"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )

        opencode_go_module._warned_unmarked_models.clear()
        with caplog.at_level("WARNING", logger="vbot.providers.opencode_go"):
            for _ in range(3):
                await opencode_go_adapter.send(
                    [{"role": "user", "content": "hello"}],
                    model_id="repeated-unlisted-model",
                )

        warnings = [
            record
            for record in caplog.records
            if "no metadata protocol" in record.getMessage()
            and "repeated-unlisted-model" in record.getMessage()
        ]
        assert len(warnings) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_replays_reasoning_meta_for_all_assistants(
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
        older_thinking = [
            block
            for block in older_blocks
            if isinstance(block, dict) and block.get("type") == "thinking"
        ]
        latest_thinking = [
            block
            for block in latest_blocks
            if isinstance(block, dict) and block.get("type") == "thinking"
        ]
        assert older_thinking == [
            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
        ]
        assert latest_thinking == [
            {"type": "thinking", "thinking": "latest thinking", "signature": "sig-latest"}
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_passes_assistant_reasoning_through_unchanged(
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
        assert any(
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
    async def test_minimax_stream_replays_reasoning_meta_for_all_assistants(
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
        assert any(
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
