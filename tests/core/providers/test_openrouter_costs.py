"""Costs on both live-verified OpenRouter wires, including streaming persistence."""

import json

import httpx
import pytest
import respx

from core.chat.streaming import StreamingAccumulator
from tests.core.providers.openrouter_test_support import (
    CHAT_URL,
    HELLO,
    RESPONSES_URL,
    chat_sse,
    openrouter_adapter,
    responses_sse,
)

UPSTREAM_COST = 0.000168


@pytest.mark.asyncio
@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cost", [0, 0.0000084])
@pytest.mark.parametrize("byok", [False, True])
@respx.mock
async def test_cost_survives_both_endpoint_families(responses, streaming, cost, byok):
    adapter = openrouter_adapter()
    details_key = "input_tokens_details" if responses else "prompt_tokens_details"
    usage = {
        ("input_tokens" if responses else "prompt_tokens"): 12,
        ("output_tokens" if responses else "completion_tokens"): 5,
        "cost": cost,
        "is_byok": byok,
        # BYOK: ``cost`` is OpenRouter's fee and the upstream Provider bills
        # this amount to the user's own key. Otherwise ``cost`` includes it.
        "cost_details": {"upstream_inference_cost": UPSTREAM_COST},
        details_key: {"cached_tokens": 2, "cache_write_tokens": 0},
    }
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
    url = RESPONSES_URL if responses else CHAT_URL
    if streaming:
        route = respx.post(url).mock(
            return_value=(
                responses_sse(
                    ("response.output_text.delta", {"delta": "OK"}),
                    ("response.completed", {"response": response}),
                )
                if responses
                else chat_sse(
                    {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": usage},
                )
            )
        )
        accumulator = StreamingAccumulator()
        async for delta in adapter.stream(HELLO, model_id=model):
            accumulator.add_delta(delta)
        normalized_usage = accumulator.finalize_assistant_fields().usage
        if not responses:
            # The Chat Completions usage chunk only arrives when requested.
            body = json.loads(route.calls.last.request.content)
            assert body["stream_options"] == {"include_usage": True}
    else:
        respx.post(url).mock(return_value=httpx.Response(200, json=response))
        raw = await adapter.send(HELLO, model_id=model)
        normalized_usage = adapter.normalize_response(raw, model_id=model)["usage"]
    expected_cost = cost + UPSTREAM_COST if byok else cost
    assert normalized_usage["reported_cost_usd"] == pytest.approx(expected_cost)
    assert normalized_usage["cache_read_tokens"] == 2
    assert normalized_usage["cache_write_tokens"] == 0


# The full invalid-money vocabulary is owned by test_pricing.py.
@pytest.mark.parametrize("cost", [None, -1, True])
def test_invalid_provider_cost_is_omitted(cost):
    normalized = openrouter_adapter().normalize_response(
        {
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "cost": cost},
        }
    )
    assert "reported_cost_usd" not in normalized["usage"]


@pytest.mark.parametrize(
    "cost_details",
    [{"upstream_inference_cost": None}, {"upstream_inference_cost": -1}, {}, None],
)
def test_byok_without_a_valid_upstream_cost_keeps_the_openrouter_cost(cost_details):
    normalized = openrouter_adapter().normalize_response(
        {
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "cost": 0.0000084,
                "is_byok": True,
                "cost_details": cost_details,
            },
        }
    )
    assert normalized["usage"]["reported_cost_usd"] == 0.0000084
