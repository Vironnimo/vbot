"""Ollama: cloud behavior."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from PIL import Image

from core.attachments.images import ImageConverter
from core.chat.block_resolver import ContentBlockResolver
from core.chat.messages import ChatMessage
from core.chat.wire_shaping import (
    _assemble_request_history,
    _assistant_continuation_dict,
    _assistant_message_from_response,
)
from core.models.models import (
    ModelRegistry,
)
from core.providers.errors import ProviderRequestTooLargeError
from core.providers.ollama import (
    OLLAMA_CLOUD_MODE,
    OllamaCloudAdapter,
)
from core.providers.providers import ProviderRegistry
from tests.core.providers.ollama_helpers import (
    CLOUD_TEXT_RESPONSE,
    OLLAMA_CLOUD_CHAT_URL,
    OLLAMA_CLOUD_CONFIG,
    SAMPLE_MESSAGES,
    _last_request_payload,
    _model_lookup,
)
from tests.core.providers.ollama_helpers import (
    adapter as adapter,
)


@pytest.fixture
def cloud_adapter() -> OllamaCloudAdapter:
    return OllamaCloudAdapter(
        OLLAMA_CLOUD_CONFIG,
        "ollama-secret",
        model_lookup=_model_lookup,
        connection_mode=OLLAMA_CLOUD_MODE,
    )


CLOUD_REASONING_CONTENT_RESPONSE: dict[str, Any] = {
    **CLOUD_TEXT_RESPONSE,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "OK",
                "reasoning_content": "The user requested exactly OK.",
            },
            "finish_reason": "stop",
        }
    ],
}


class TestOllamaCloudChatWire:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("streaming", [False, True])
    @pytest.mark.parametrize("delta", [-1, 0, 1])
    async def test_request_body_limit_counts_exact_wire_bytes_before_io(
        self, cloud_adapter: OllamaCloudAdapter, streaming: bool, delta: int
    ) -> None:
        limit = cloud_adapter.request_body_limit("glm-5.3-flash")
        assert limit == 16 * 1024 * 1024
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": 'Grüße 😀 "\\\n'},
                    {"type": "media", "media_type": "image/png", "base64": ""},
                ],
            }
        ]
        kwargs = {
            "max_tokens": 1,
            "tools": [
                {
                    "name": "test",
                    "description": "Tool ä",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
        payload = cloud_adapter._build_payload(messages, "glm-5.3-flash", **kwargs)
        if streaming:
            cloud_adapter._prepare_stream_payload(payload)
        overhead = len(httpx.Request("POST", OLLAMA_CLOUD_CHAT_URL, json=payload).content)
        messages[0]["content"][1]["base64"] = "A" * (limit + delta - overhead)
        sse = (
            'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        with respx.mock(assert_all_called=False) as router:
            route = router.post(OLLAMA_CLOUD_CHAT_URL).mock(
                return_value=(
                    httpx.Response(200, text=sse)
                    if streaming
                    else httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
                )
            )

            async def invoke() -> None:
                if streaming:
                    _ = [
                        chunk
                        async for chunk in cloud_adapter.stream(
                            messages, model_id="glm-5.3-flash", **kwargs
                        )
                    ]
                else:
                    await cloud_adapter.send(messages, model_id="glm-5.3-flash", **kwargs)

            try:
                if delta > 0:
                    with pytest.raises(ProviderRequestTooLargeError) as failure:
                        await invoke()
                    assert failure.value.size_bytes == limit + delta
                    assert failure.value.max_bytes == limit
                    assert failure.value.retryable is False
                    assert route.call_count == 0
                else:
                    await invoke()
                    assert route.call_count == 1
                    assert len(route.calls.last.request.content) == limit + delta
            finally:
                await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_v41_image_profile_converts_gif_before_cloud_request(self) -> None:
        resources = Path(__file__).resolve().parents[3] / "resources"
        models = ModelRegistry.load(resources)
        config = ProviderRegistry.load(resources).get("ollama-cloud")
        adapter = OllamaCloudAdapter(
            config, "test-key", model_lookup=lambda mid: models.get("ollama-cloud", mid)
        )
        model_id = "deepseek-v4.1-flash"
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )
        source = io.BytesIO()
        Image.new("RGB", (12, 8), "red").save(source, format="GIF")
        original = {
            "path": "fixture.gif",
            "filename": "fixture.gif",
            "media_type": "image/gif",
            "base64": base64.b64encode(source.getvalue()).decode("ascii"),
        }
        try:
            supported = adapter.wire_media_support(model_id)
            assert supported == frozenset({"image/png", "image/jpeg", "image/webp"})
            assert "image/gif" in adapter.wire_media_support("deepseek-v4-flash:0731")
            parts = await ContentBlockResolver.resolve_tool_image(
                original, frozenset({"text", "image"}), supported, ImageConverter()
            )
            assert parts[0]["media_type"] == "image/png"
            assert original["media_type"] == "image/gif"
            with Image.open(io.BytesIO(base64.b64decode(parts[0]["base64"]))) as converted:
                assert converted.size == (12, 8)
                assert converted.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
            await adapter.send([{"role": "user", "content": parts}], model_id=model_id)
            payload = _last_request_payload(route)
            assert payload["messages"][0]["content"][0]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
        finally:
            await adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize("current_run", [False, True])
    @pytest.mark.parametrize(
        ("effort", "wire_effort"),
        [("low", "low"), ("high", "high"), ("max", "max"), ("xhigh", "max"), ("none", "none")],
    )
    async def test_v41_replays_persisted_reasoning_on_fresh_adapter(
        self, current_run: bool, effort: str, wire_effort: str
    ) -> None:
        """Protect the locally serialized contract verified live on 2026-09-11."""
        resources = Path(__file__).resolve().parents[3] / "resources"
        models = ModelRegistry.load(resources)
        config = ProviderRegistry.load(resources).get("ollama-cloud")
        connection = config.get_connection("api-key")
        model_id = "deepseek-v4.1-flash"
        scope = f"ollama-cloud/{model_id}::api-key"
        adapter = OllamaCloudAdapter(
            config,
            "test-key",
            connection.base_url,
            connection.auth,
            model_lookup=lambda mid: models.get("ollama-cloud", mid),
            connection_mode=connection.mode,
        )
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )
        # Simulate the normalized response persisted by a previous Adapter/Run.
        assistant = _assistant_message_from_response(
            f"ollama-cloud/{model_id}",
            {
                "content": None,
                "reasoning": "test-owned reasoning sentinel",
                "reasoning_meta": {"reasoning_details": [{"text": "opaque sentinel"}]},
                "tool_calls": [
                    {"id": "call_weather", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            reasoning_scope=scope,
        )
        restored = ChatMessage.from_dict(json.loads(json.dumps(assistant.to_dict())))
        policy = adapter.reasoning_replay_policy(model_id)
        assert policy == "full_history"
        assert adapter.reasoning_replay_fidelity(model_id) == "readable_only"
        messages = _assemble_request_history(
            [ChatMessage.user("Check Berlin weather."), restored],
            replay_policy=policy,
            agent_model=scope,
        )
        if current_run:
            messages[-1] = _assistant_continuation_dict(restored, replay_policy=policy)
        messages.append({"role": "tool", "tool_call_id": "call_weather", "content": "20 C"})
        if not current_run:
            messages.extend(
                [
                    {"role": "assistant", "content": "20 C in Berlin."},
                    {"role": "user", "content": "Check again."},
                ]
            )
        try:
            await adapter.send(
                messages,
                model_id=model_id,
                thinking_effort=effort,
                tools=[
                    {
                        "name": "get_weather",
                        "description": "Test weather tool",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    }
                ],
            )
            payload = _last_request_payload(route)
            replayed = payload["messages"][1]
            assert payload["model"] == model_id
            assert payload["reasoning_effort"] == wire_effort
            assert payload["max_tokens"] == 393_216
            assert payload["tools"][0]["function"]["name"] == "get_weather"
            assert replayed["reasoning"] == "test-owned reasoning sentinel"
            assert "reasoning_content" not in replayed
            assert "reasoning_details" not in replayed
            assert "reasoning_scope" not in replayed
            assert "opaque sentinel" not in json.dumps(payload)
            assert replayed["tool_calls"][0]["id"] == payload["messages"][2]["tool_call_id"]
        finally:
            await adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
    async def test_standard_efforts_use_openai_chat_completions(
        self,
        cloud_adapter: OllamaCloudAdapter,
        effort: str,
    ) -> None:
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )

        await cloud_adapter.send(
            SAMPLE_MESSAGES,
            model_id="minimax-m3",
            thinking_effort=effort,
        )

        payload = _last_request_payload(route)
        assert payload["reasoning_effort"] == effort
        assert route.calls.last.request.headers["Authorization"] == "Bearer ollama-secret"
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_xhigh_maps_to_cloud_max(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )

        await cloud_adapter.send(
            SAMPLE_MESSAGES,
            model_id="deepseek-v4-flash",
            thinking_effort="xhigh",
        )

        assert _last_request_payload(route)["reasoning_effort"] == "max"
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_none_sends_explicit_cloud_off_switch_for_on_off_model(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )

        await cloud_adapter.send(
            SAMPLE_MESSAGES,
            model_id="thinking-model",
            thinking_effort="none",
        )

        assert _last_request_payload(route)["reasoning_effort"] == "none"
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_thinking_model_omits_reasoning_effort(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )

        await cloud_adapter.send(
            SAMPLE_MESSAGES,
            model_id="plain-model",
            thinking_effort="high",
        )

        assert "reasoning_effort" not in _last_request_payload(route)
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_model_omits_reasoning_effort_until_catalog_confirms_support(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )

        await cloud_adapter.send(
            SAMPLE_MESSAGES,
            model_id="new-unenriched-model",
            thinking_effort="high",
        )

        assert "reasoning_effort" not in _last_request_payload(route)
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_effort_is_omitted_instead_of_rejected(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )

        await cloud_adapter.send(
            SAMPLE_MESSAGES,
            model_id="minimax-m3",
            thinking_effort="future-tier",
        )

        assert "reasoning_effort" not in _last_request_payload(route)
        await cloud_adapter.aclose()

    def test_response_keeps_reasoning_and_measured_output_but_drops_zero_input(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        normalized = cloud_adapter.normalize_response(
            CLOUD_TEXT_RESPONSE,
            model_id="minimax-m3",
        )

        assert normalized["reasoning"] == "The user requested exactly OK."
        assert normalized["usage"] == {"output_tokens": 9}

    def test_response_preserves_positive_prompt_usage(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        response = {
            **CLOUD_TEXT_RESPONSE,
            "usage": {"prompt_tokens": 2975, "completion_tokens": 25, "total_tokens": 3000},
        }

        normalized = cloud_adapter.normalize_response(response, model_id="minimax-m3")

        assert normalized["usage"] == {"input_tokens": 2975, "output_tokens": 25}

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_surfaces_reasoning_and_omits_zero_input_usage(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        sse_body = "".join(
            (
                'data: {"choices":[{"delta":{"reasoning":"Check."}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n',
                'data: {"choices":[],"usage":{"prompt_tokens":0,'
                '"completion_tokens":22,"total_tokens":0}}\n\n',
                "data: [DONE]\n\n",
            )
        )
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        deltas = [
            delta
            async for delta in cloud_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="minimax-m3",
                thinking_effort="high",
            )
        ]

        assert {"type": "reasoning_delta", "text": "Check."} in deltas
        assert {"type": "content_delta", "text": "OK"} in deltas
        assert {"type": "usage", "output_tokens": 22} in deltas
        payload = _last_request_payload(route)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_continuation_uses_openai_compatible_message_shape(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )
        messages: list[dict[str, Any]] = [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_weather",
                        "name": "get_weather",
                        "arguments": {"city": "Berlin"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_weather",
                "name": "get_weather",
                "content": '{"temperature": 24}',
            },
        ]

        await cloud_adapter.send(messages, model_id="minimax-m3", thinking_effort="high")

        payload_messages = _last_request_payload(route)["messages"]
        assistant_tool_call = payload_messages[-2]["tool_calls"][0]
        assert assistant_tool_call["function"]["arguments"] == '{"city":"Berlin"}'
        assert payload_messages[-1]["tool_call_id"] == "call_weather"
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_top_p_rides_in_cloud_payload(self, cloud_adapter: OllamaCloudAdapter) -> None:
        """top_p reaches the OpenAI-compatible Cloud request body."""
        # Arrange
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )

        # Act
        await cloud_adapter.send(SAMPLE_MESSAGES, model_id="deepseek-v4-flash:0731", top_p=0.95)

        # Assert
        payload = _last_request_payload(route)
        assert payload["top_p"] == 0.95
        await cloud_adapter.aclose()

    def test_glm_cloud_reasoning_replay_policy_is_full_history(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """GLM-5.x Cloud Models replay reasoning across turns (template retains thinking)."""

        assert cloud_adapter.reasoning_replay_policy("glm-5.2") == "full_history"

    def test_unprofiled_cloud_reasoning_replay_defaults_to_full_history(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        assert cloud_adapter.reasoning_replay_policy("unknown-cloud-model") == "full_history"

    def test_kimi_cloud_reasoning_replay_is_current_run(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """Kimi K2.6 Cloud replays reasoning only within the current run.

        Live-verified 2026-08-26 via streaming token accounting: the /v1 wire
        accepts and bills in-run replayed reasoning under the ``reasoning``
        carrier, while cross-run replay is stripped.
        """

        assert cloud_adapter.reasoning_replay_policy("kimi-k2.6") == "current_run"

    def test_minimax_m2_cloud_reasoning_replay_is_none(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """MiniMax M2.7 Cloud strips replayed reasoning on every carrier.

        Live-verified 2026-08-26 via streaming token accounting: zero billed
        delta for the ``reasoning`` carrier in both scopes while the visible
        control validates accounting.
        """

        assert cloud_adapter.reasoning_replay_policy("minimax-m2.7") == "none"

    def test_deepseek_cloud_reasoning_replay_is_full_history(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """DeepSeek V4 Cloud replays all Reasoning when Tools are present.

        Live-verified 2026-09-02 via streaming token accounting: Ollama Cloud
        bills cross-Run ``reasoning`` when the current request carries Tools,
        matching DeepSeek's contract. Tool-free requests ignore it upstream.
        """

        assert cloud_adapter.reasoning_replay_policy("deepseek-v4-flash:0731") == "full_history"

    @respx.mock
    @pytest.mark.asyncio
    async def test_deepseek_cloud_replays_reasoning_as_reasoning(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """Ollama Cloud translates DeepSeek replay to ``reasoning``.

        The upstream DeepSeek field is ``reasoning_content``; live Ollama Cloud
        responses and accepted historical input use ``reasoning`` instead.
        """

        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )
        messages: list[dict[str, Any]] = [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "model": "deepseek-v4-flash:0731",
                "content": "OK",
                "reasoning": "The user requested exactly OK.",
            },
        ]

        await cloud_adapter.send(
            messages, model_id="deepseek-v4-flash:0731", thinking_effort="high"
        )

        payload_messages = _last_request_payload(route)["messages"]
        assistant_message = payload_messages[-1]
        assert assistant_message["reasoning"] == "The user requested exactly OK."
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_glm_cloud_replays_reasoning_as_reasoning(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """Live-verified GLM reasoning replays under Ollama Cloud's ``reasoning`` field."""

        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )
        messages: list[dict[str, Any]] = [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "model": "glm-5.2",
                "content": "OK",
                "reasoning": "The user requested exactly OK.",
            },
        ]

        await cloud_adapter.send(messages, model_id="glm-5.2", thinking_effort="high")

        payload_messages = _last_request_payload(route)["messages"]
        assistant_message = payload_messages[-1]
        assert assistant_message["reasoning"] == "The user requested exactly OK."
        assert assistant_message["content"] == "OK"
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_glm_cloud_replay_reasoning_native_field_only(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """GLM-5.2 replays reasoning via the native field without content injection."""

        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )
        messages: list[dict[str, Any]] = [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "model": "glm-5.2",
                "content": "OK",
                "reasoning": "The user requested exactly OK.",
            },
        ]

        await cloud_adapter.send(messages, model_id="glm-5.2", thinking_effort="high")

        assistant_message = _last_request_payload(route)["messages"][-1]
        assert assistant_message["reasoning"] == "The user requested exactly OK."
        assert assistant_message["content"] == "OK"
        assert "<reasoning_history>" not in assistant_message["content"]
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_unprofiled_cloud_replay_defaults_to_reasoning_content(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """The first replay of an unprofiled Model uses the de-facto standard field."""

        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )
        messages: list[dict[str, Any]] = [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "model": "unprofiled-cloud-model",
                "content": "OK",
                "reasoning": "EXACT old Reasoning: äöü\nline two\n",
            },
        ]

        await cloud_adapter.send(
            messages, model_id="unprofiled-cloud-model", thinking_effort="high"
        )

        payload_messages = _last_request_payload(route)["messages"]
        assistant_message = payload_messages[-1]
        assert assistant_message["reasoning_content"] == "EXACT old Reasoning: äöü\nline two\n"
        assert "reasoning" not in assistant_message
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_unprofiled_cloud_replay_uses_scanned_reasoning_content_field(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """An unprofiled Model whose response carries reasoning_content replays it there."""

        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_REASONING_CONTENT_RESPONSE)
        )
        raw_response = await cloud_adapter.send(
            SAMPLE_MESSAGES, model_id="unprofiled-cloud-model", thinking_effort="high"
        )
        # Chat normalizes every send() response before the next request.
        cloud_adapter.normalize_response(raw_response, model_id="unprofiled-cloud-model")

        route.mock(return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE))
        messages: list[dict[str, Any]] = [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "model": "unprofiled-cloud-model",
                "content": "OK",
                "reasoning": "EXACT old Reasoning: äöü\nline two\n",
            },
        ]
        await cloud_adapter.send(
            messages, model_id="unprofiled-cloud-model", thinking_effort="high"
        )

        payload_messages = _last_request_payload(route)["messages"]
        assistant_message = payload_messages[-1]
        assert assistant_message["reasoning_content"] == "EXACT old Reasoning: äöü\nline two\n"
        assert "reasoning" not in assistant_message
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_unprofiled_cloud_replay_uses_scanned_reasoning_field(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """An unprofiled Model whose response carries reasoning replays it there."""

        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )
        raw_response = await cloud_adapter.send(
            SAMPLE_MESSAGES, model_id="unprofiled-cloud-model", thinking_effort="high"
        )
        # Chat normalizes every send() response before the next request.
        cloud_adapter.normalize_response(raw_response, model_id="unprofiled-cloud-model")

        route.mock(return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE))
        messages: list[dict[str, Any]] = [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "model": "unprofiled-cloud-model",
                "content": "OK",
                "reasoning": "EXACT old Reasoning: äöü\nline two\n",
            },
        ]
        await cloud_adapter.send(
            messages, model_id="unprofiled-cloud-model", thinking_effort="high"
        )

        payload_messages = _last_request_payload(route)["messages"]
        assistant_message = payload_messages[-1]
        assert assistant_message["reasoning"] == "EXACT old Reasoning: äöü\nline two\n"
        assert "reasoning_content" not in assistant_message
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_unprofiled_cloud_stream_scan_drives_replay_field(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """A streamed reasoning_content response steers the next replay's field."""

        sse_body = "".join(
            (
                'data: {"choices":[{"delta":{"reasoning_content":"Check."}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n',
                'data: {"choices":[],"usage":{"prompt_tokens":0,'
                '"completion_tokens":22,"total_tokens":0}}\n\n',
                "data: [DONE]\n\n",
            )
        )
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        deltas = [
            delta
            async for delta in cloud_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="unprofiled-cloud-model",
                thinking_effort="high",
            )
        ]

        assert {"type": "reasoning_delta", "text": "Check."} in deltas
        assert {"type": "content_delta", "text": "OK"} in deltas
        assert {"type": "usage", "output_tokens": 22} in deltas

        route.mock(return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE))
        replay_messages: list[dict[str, Any]] = [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "model": "unprofiled-cloud-model",
                "content": "OK",
                "reasoning": "EXACT old Reasoning: äöü\nline two\n",
            },
        ]
        await cloud_adapter.send(
            replay_messages, model_id="unprofiled-cloud-model", thinking_effort="high"
        )

        payload_messages = _last_request_payload(route)["messages"]
        assistant_message = payload_messages[-1]
        assert assistant_message["reasoning_content"] == "EXACT old Reasoning: äöü\nline two\n"
        assert "reasoning" not in assistant_message
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_kimi_cloud_replays_reasoning_as_reasoning_field(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """Kimi Cloud models emit and accept ``reasoning`` as the replay carrier.

        Live-verified 2026-08-26: the /v1 wire strips ``reasoning_content``
        even in-run, so the profiled field must be ``reasoning``.
        """

        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )
        messages: list[dict[str, Any]] = [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "model": "kimi-k2.6",
                "content": "OK",
                "reasoning": "The user requested exactly OK.",
            },
        ]

        await cloud_adapter.send(messages, model_id="kimi-k2.6", thinking_effort="high")

        payload_messages = _last_request_payload(route)["messages"]
        assistant_message = payload_messages[-1]
        assert assistant_message["reasoning"] == "The user requested exactly OK."
        assert "reasoning_content" not in assistant_message
        await cloud_adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_m3_cloud_replays_reasoning_as_reasoning_field(
        self,
        cloud_adapter: OllamaCloudAdapter,
    ) -> None:
        """MiniMax M3 returns reasoning as ``reasoning`` and requires it for continuity."""

        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )
        messages: list[dict[str, Any]] = [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "model": "minimax-m3",
                "content": "OK",
                "reasoning": "The user requested exactly OK.",
            },
        ]

        await cloud_adapter.send(messages, model_id="minimax-m3", thinking_effort="high")

        payload_messages = _last_request_payload(route)["messages"]
        assistant_message = payload_messages[-1]
        assert assistant_message["reasoning"] == "The user requested exactly OK."
        assert "reasoning_content" not in assistant_message
        await cloud_adapter.aclose()
