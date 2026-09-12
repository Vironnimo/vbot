"""Opencode go: protocol routing behavior."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

import core.providers.opencode_go as opencode_go_module
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.anthropic_compatible import AnthropicCompatibleAdapter
from core.providers.errors import ProviderError
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import (
    OPENCODE_SESSION_HEADER,
    OpenCodeGoAdapter,
)
from core.providers.providers import AuthConfig, ProviderConfig
from tests.core.providers.opencode_go_helpers import (
    ANTHROPIC_MESSAGES_MODELS,
    API_KEY,
    CLOSED_TOOL,
    OPENCODE_GO_MESSAGES_URL,
    OPENCODE_GO_RESPONSES_URL,
    OPENCODE_GO_URL,
)
from tests.core.providers.opencode_go_helpers import (
    opencode_go_adapter as opencode_go_adapter,
)
from tests.core.providers.opencode_go_helpers import (
    opencode_go_config as opencode_go_config,
)


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
            assert isinstance(adapter._messages, AnthropicCompatibleAdapter)
            assert adapter._messages._client is adapter._client
            assert adapter._messages._token_getter is adapter._token_getter
            assert adapter._messages._auth_config.header == "x-api-key"
            assert adapter._messages._auth_config.prefix == ""
            assert adapter._messages._auth_config.credential_key == runtime_auth.credential_key
        finally:
            await adapter.aclose()

    def test_wire_media_support_routes_by_model_protocol(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        assert opencode_go_adapter.wire_media_support("minimax-m2.7") == (IMAGE_WIRE_MEDIA_TYPES)
        assert opencode_go_adapter.wire_media_support("deepseek-v4-flash") == (
            OpenAICompatibleAdapter.wire_media_support(
                opencode_go_adapter,
                "deepseek-v4-flash",
            )
        )

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
        request = messages_route.calls.last.request
        assert request.headers["x-api-key"] == API_KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["user-agent"] == "vBot"
        body = json.loads(request.content)
        assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_path_never_enables_strict_mode(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "inspect"}],
            model_id="minimax-m3",
            tools=[CLOSED_TOOL],
        )

        body = json.loads(messages_route.calls.last.request.content)
        assert body["tools"] == [
            {
                "name": CLOSED_TOOL["name"],
                "description": CLOSED_TOOL["description"],
                "input_schema": CLOSED_TOOL["parameters"],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_path_keeps_large_tool_set_non_strict(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )
        )
        tools = [{**CLOSED_TOOL, "name": f"inspect_probe_{index}"} for index in range(21)]

        await opencode_go_adapter.send(
            [{"role": "user", "content": "inspect"}],
            model_id="minimax-m3",
            tools=tools,
        )

        body = json.loads(messages_route.calls.last.request.content)
        assert len(body["tools"]) == 21
        assert all("strict" not in tool for tool in body["tools"])

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
        assert chat_route.calls.last.request.headers["user-agent"] == "vBot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_path_omits_unsupported_strict_field(
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

        await opencode_go_adapter.send(
            [{"role": "user", "content": "inspect"}],
            model_id="deepseek-v4-flash",
            tools=[CLOSED_TOOL],
        )

        body = json.loads(chat_route.calls.last.request.content)
        assert "strict" not in body["tools"][0]["function"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_path_does_not_retry_permanent_subscription_limit(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "error": {
                        "type": "GoUsageLimitError",
                        "message": "Monthly usage limit reached. Enable available balance.",
                    }
                },
            )
        )

        with pytest.raises(ProviderError, match="subscription limit reached") as exc_info:
            await opencode_go_adapter.send(
                [{"role": "user", "content": "hello"}],
                model_id="deepseek-v4-flash",
            )

        assert exc_info.value.retryable is False
        assert chat_route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_path_does_not_retry_permanent_subscription_limit(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "type": "error",
                    "error": {
                        "type": "FreeUsageLimitError",
                        "message": "Monthly usage limit has been reached.",
                    },
                },
            )
        )

        with pytest.raises(ProviderError, match="subscription limit reached") as exc_info:
            await opencode_go_adapter.send(
                [{"role": "user", "content": "hello"}],
                model_id="minimax-m3",
            )

        assert exc_info.value.retryable is False
        assert messages_route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_stream_does_not_retry_permanent_subscription_limit(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "error": {
                        "code": "insufficient_quota",
                        "message": "Quota exceeded.",
                    }
                },
            )
        )

        with pytest.raises(ProviderError, match="subscription limit reached") as exc_info:
            async for _ in opencode_go_adapter.stream(
                [{"role": "user", "content": "hello"}],
                model_id="deepseek-v4-flash",
            ):
                pass

        assert exc_info.value.retryable is False
        assert chat_route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_stream_does_not_retry_permanent_subscription_limit(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "type": "error",
                    "error": {
                        "type": "GoUsageLimitError",
                        "message": "Use available balance to continue.",
                    },
                },
            )
        )

        with pytest.raises(ProviderError, match="subscription limit reached") as exc_info:
            async for _ in opencode_go_adapter.stream(
                [{"role": "user", "content": "hello"}],
                model_id="minimax-m3",
            ):
                pass

        assert exc_info.value.retryable is False
        assert messages_route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_path_still_retries_transient_rate_limit(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={"error": {"type": "rate_limit_error", "message": "Slow down."}},
                ),
                httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                ),
            ]
        )

        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            response = await opencode_go_adapter.send(
                [{"role": "user", "content": "hello"}],
                model_id="deepseek-v4-flash",
                **opencode_go_adapter.request_context_kwargs(
                    project_id="vbot",
                    agent_id="builder",
                    session_id="session",
                    prompt_cache_affinity_id="retry-affinity",
                ),
            )

        assert response["choices"][0]["message"]["content"] == "ok"
        assert chat_route.call_count == 2
        assert all(
            call.request.headers[OPENCODE_SESSION_HEADER] == "vbot-retry-affinity"
            for call in chat_route.calls
        )

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
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(
                200,
                json={"id": "resp_x", "status": "completed", "output": []},
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
        assert not responses_route.called
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
            },
            model_id="deepseek-v4-flash",
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

    def test_normalize_response_routes_by_model_when_shape_is_ambiguous(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        result = opencode_go_adapter.normalize_response(
            {
                "choices": [{"message": {"role": "assistant", "content": "wrong wire"}}],
                "content": [{"type": "text", "text": "messages wire"}],
            },
            model_id="minimax-m2.7",
        )

        assert result["content"] == "messages wire"

    @pytest.mark.asyncio
    async def test_aclose_closes_shared_client_once(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        shared_client = AsyncMock()
        opencode_go_adapter._client = shared_client
        opencode_go_adapter._messages._client = shared_client

        await opencode_go_adapter.aclose()

        shared_client.aclose.assert_awaited_once()
