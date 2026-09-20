"""Costs on both live-verified OpenRouter wires, including streaming persistence."""

import json

import httpx
import pytest
import respx

from core.chat.streaming import StreamingAccumulator
from tests.core.providers.openrouter_helpers import (
    OPENROUTER_RESPONSES_URL,
    OPENROUTER_URL,
    SAMPLE_MESSAGES,
)
from tests.core.providers.openrouter_helpers import (
    openrouter_adapter as openrouter_adapter,
)
from tests.core.providers.openrouter_helpers import (
    openrouter_config as openrouter_config,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cost", [0, 0.0000084])
@respx.mock
async def test_cost_survives_both_endpoint_families(openrouter_adapter, responses, streaming, cost):
    usage = (
        {
            "input_tokens": 12,
            "output_tokens": 5,
            "cost": cost,
            "cost_details": {"upstream_inference_cost": 99},
            "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 0},
        }
        if responses
        else {
            "prompt_tokens": 12,
            "completion_tokens": 5,
            "cost": cost,
            "cost_details": {"upstream_inference_cost": 99},
            "prompt_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 0},
        }
    )
    response = (
        {
            "id": "resp_cost",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}],
                }
            ],
            "usage": usage,
        }
        if responses
        else {
            "choices": [
                {"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
            ],
            "usage": usage,
        }
    )
    model = "openai/gpt-5.6-luna" if responses else "google/gemini-2.5-flash-lite"
    url = OPENROUTER_RESPONSES_URL if responses else OPENROUTER_URL
    if streaming:
        if responses:
            body = (
                'event: response.output_text.delta\ndata: {"delta":"OK"}\n\n'
                + "event: response.completed\ndata: "
                + json.dumps({"response": response})
                + "\n\n"
            )
        else:
            body = (
                'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
                + "data: "
                + json.dumps({"choices": [], "usage": usage})
                + "\n\ndata: [DONE]\n\n"
            )
        respx.post(url).mock(
            return_value=httpx.Response(
                200, text=body, headers={"content-type": "text/event-stream"}
            )
        )
        accumulator = StreamingAccumulator()
        async for delta in openrouter_adapter.stream(SAMPLE_MESSAGES, model_id=model):
            accumulator.add_delta(delta)
        normalized_usage = accumulator.finalize_assistant_fields().usage
    else:
        respx.post(url).mock(return_value=httpx.Response(200, json=response))
        raw = await openrouter_adapter.send(SAMPLE_MESSAGES, model_id=model)
        normalized_usage = openrouter_adapter.normalize_response(raw, model_id=model)["usage"]
    assert normalized_usage["reported_cost_usd"] == cost
    assert normalized_usage["cache_read_tokens"] == 2
    assert normalized_usage["cache_write_tokens"] == 0


@pytest.mark.parametrize("cost", [None, -1, True, "0.25"])
def test_invalid_provider_cost_is_omitted(openrouter_adapter, cost):
    normalized = openrouter_adapter.normalize_response(
        {
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "cost": cost},
        }
    )
    assert "reported_cost_usd" not in normalized["usage"]
