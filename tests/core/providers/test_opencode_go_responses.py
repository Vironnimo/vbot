"""Opencode go: responses behavior."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

import core.providers.opencode_go as opencode_go_module
from core.providers.github_copilot_responses import estimate_responses_input_tokens
from core.providers.opencode_go import (
    OpenCodeGoAdapter,
)
from core.utils.tokens import estimate_request_input_tokens
from tests.core.providers.opencode_go_helpers import (
    CLOSED_TOOL,
    OPENCODE_GO_MESSAGES_URL,
    OPENCODE_GO_RESPONSES_URL,
    OPENCODE_GO_URL,
    RESPONSES_COMPLETED_RESPONSE,
)
from tests.core.providers.opencode_go_helpers import (
    opencode_go_adapter as opencode_go_adapter,
)
from tests.core.providers.opencode_go_helpers import (
    opencode_go_config as opencode_go_config,
)

# Ids carrying "responses" in the protocol map below must route through the
# shared stateless Responses machinery (/responses endpoint).
RESPONSES_MODELS: tuple[str, ...] = (
    "gpt-5.6-luna",
    "grok-4.5",
    "grok-4.6",
    "muse-spark-1.2-contributor",
    "muse-spark-1.3-contributor",
)


class TestOpenCodeGoResponsesRouting:
    @pytest.mark.parametrize("model_id", RESPONSES_MODELS)
    def test_responses_models_resolve_responses_protocol(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        assert opencode_go_adapter._model_protocol(model_id) == "responses"

    def test_current_chat_and_messages_models_resolve_documented_protocols(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        assert opencode_go_adapter._model_protocol("longcat-2.0") == "openai"
        assert opencode_go_adapter._model_protocol("hy4-preview") == "openai"
        assert opencode_go_adapter._model_protocol("qwen3.8-flash") == "anthropic"
        assert opencode_go_adapter._model_protocol("qwen3.8-max") == "anthropic"

    def test_responses_context_estimate_uses_rendered_responses_items(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "Keep answers concise."},
            {"role": "user", "content": "Inspect the repository."},
            {
                "role": "assistant",
                "content": None,
                "reasoning_meta": {
                    "response_output": [
                        {
                            "type": "reasoning",
                            "encrypted_content": "opaque-continuity",
                        }
                    ],
                    "reasoning_items": [{"type": "reasoning", "text": "duplicated " * 20_000}],
                    "encrypted_content": ["duplicated " * 20_000],
                },
            },
        ]

        estimated = opencode_go_adapter.estimate_request_input_tokens(
            messages,
            model_id="muse-spark-1.2-contributor",
            tools=[CLOSED_TOOL],
        )
        raw_chat_estimate, _ = estimate_request_input_tokens(messages, [CLOSED_TOOL])

        assert estimated == estimate_responses_input_tokens(messages, tools=[CLOSED_TOOL])
        assert raw_chat_estimate < 1_000

    @respx.mock
    @pytest.mark.asyncio
    async def test_responses_output_limit_uses_responses_context_estimate(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )
        with patch.object(
            opencode_go_module,
            "estimate_responses_input_tokens",
            return_value=700_000,
        ) as estimator:
            await opencode_go_adapter.send(
                [{"role": "user", "content": "hello"}],
                model_id="muse-spark-1.2-contributor",
            )

        assert responses_route.called
        estimator.assert_called_once()

    @respx.mock
    @pytest.mark.asyncio
    async def test_responses_model_send_uses_responses_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(200, json={"choices": []})
        )
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"type": "message"})
        )

        response = await opencode_go_adapter.send(
            [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "hello"},
            ],
            model_id="gpt-5.6-luna",
        )

        assert responses_route.called
        assert not chat_route.called
        assert not messages_route.called
        request_body = json.loads(responses_route.calls.last.request.content)
        assert responses_route.calls.last.request.headers["user-agent"] == "vBot"
        # Stateless shape: complete history as input items, never stored.
        assert request_body["store"] is False
        assert request_body["instructions"] == "Be brief."
        assert [item["role"] for item in request_body["input"]] == ["user"]
        normalized = opencode_go_adapter.normalize_response(response, model_id="gpt-5.6-luna")
        assert normalized["content"] == "Done"
        assert normalized["phase"] == "final_answer"
        assert (
            normalized["reasoning_meta"]["response_output"]
            == (RESPONSES_COMPLETED_RESPONSE["output"])
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_responses_payload_renders_effort_and_encrypted_include(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="gpt-5.6-luna",
            thinking_effort="high",
            session_id="vbot-session",
            tools=[CLOSED_TOOL],
        )

        request_body = json.loads(responses_route.calls.last.request.content)
        assert request_body["reasoning"] == {"effort": "high", "summary": "auto"}
        assert request_body["include"] == ["reasoning.encrypted_content"]
        assert request_body["tools"][0]["type"] == "function"
        # Non-strict invariant: the field is carried explicitly as false.
        assert request_body["tools"][0]["strict"] is False
        # The gateway publishes no sticky-conversation contract; the routing
        # kwarg is dropped instead of leaking onto the wire.
        assert "session_id" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_gpt_luna_sends_live_verified_none_effort(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="gpt-5.6-luna",
            thinking_effort="none",
        )

        request_body = json.loads(responses_route.calls.last.request.content)
        assert request_body["reasoning"] == {"effort": "none", "summary": "auto"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_muse_omits_unsupported_none_effort(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="muse-spark-1.3-contributor",
            thinking_effort="none",
        )

        request_body = json.loads(responses_route.calls.last.request.content)
        assert "reasoning" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_grok_none_effort_maps_to_minimum_on_responses_wire(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        """The wire rejects effort ``none`` (HTTP 400); the override's minimum rung wins."""

        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="grok-4.5",
            thinking_effort="none",
        )

        request_body = json.loads(responses_route.calls.last.request.content)
        assert request_body["reasoning"] == {"effort": "low", "summary": "auto"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_responses_model_stream_happy_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        completed = {
            "id": "resp_stream",
            "object": "response",
            "status": "completed",
            "output": RESPONSES_COMPLETED_RESPONSE["output"],
            "usage": RESPONSES_COMPLETED_RESPONSE["usage"],
        }
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    'event: response.output_text.delta\ndata: {"delta":"Done"}\n\n'
                    f"event: response.completed\ndata: {json.dumps({'response': completed})}\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            )
        )

        deltas = [
            delta
            async for delta in opencode_go_adapter.stream(
                [{"role": "user", "content": "hello"}],
                model_id="muse-spark-1.2-contributor",
                thinking_effort="low",
            )
        ]

        request_body = json.loads(responses_route.calls.last.request.content)
        assert request_body["stream"] is True
        assert request_body["reasoning"] == {"effort": "low", "summary": "auto"}
        assert [delta["type"] for delta in deltas] == [
            "content_delta",
            "reasoning_meta",
            "usage",
            "finish",
        ]

    def test_normalize_response_routes_responses_shape_by_model(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        result = opencode_go_adapter.normalize_response(
            {
                "choices": [{"message": {"role": "assistant", "content": "wrong wire"}}],
                "output": RESPONSES_COMPLETED_RESPONSE["output"],
            },
            model_id="grok-4.5",
        )

        assert result["content"] == "Done"

    def test_normalize_response_infers_responses_shape_without_model_id(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        result = opencode_go_adapter.normalize_response(RESPONSES_COMPLETED_RESPONSE)

        assert result["content"] == "Done"
