"""Console billing reaches Chat through each API family's final usage carrier."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.chat.streaming import StreamingAccumulator

from .opencode_go_test_support import GEMINI_STREAM_URL, HELLO, WIRES, go_adapter, success_response


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
async def test_completed_and_streamed_requests_keep_go_allowance_separate(wire: str) -> None:
    model, url = WIRES[wire]
    adapter = go_adapter()
    complete = success_response(wire, streaming=False).json()
    usage = complete.setdefault("usageMetadata" if wire == "gemini" else "usage", {})
    usage.update(
        {"cost": 0, "opencode": {"billing_source": "go", "list_cost": 0.02, "allowance": 0.006}}
    )
    if wire == "messages":
        event = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": usage}
        stream = (
            f"event: message_delta\ndata: {json.dumps(event)}\n\n"
            'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
    elif wire == "responses":
        stream = f"event: response.completed\ndata: {json.dumps({'response': complete})}\n\n"
    elif wire == "chat":
        stream = f"data: {json.dumps({'choices': [], 'usage': usage})}\n\ndata: [DONE]\n\n"
    else:
        stream = f"data: {json.dumps(complete)}\n\n"
    with respx.mock:
        respx.post(url).respond(200, json=complete)
        result = adapter.normalize_response(
            await adapter.send(HELLO, model_id=model), model_id=model
        )
        respx.post(GEMINI_STREAM_URL if wire == "gemini" else url).respond(200, text=stream)
        accumulator = StreamingAccumulator()
        async for delta in adapter.stream(HELLO, model_id=model):
            accumulator.add_delta(delta)
    await adapter.aclose()
    observations: list[dict[str, Any] | None] = [
        result.get("usage"),
        accumulator.finalize_assistant_fields().usage,
    ]
    for observed in observations:
        assert observed and observed["reported_cost_usd"] == 0
        assert observed["cost"] == {
            "amount_usd": 0,
            "source": "provider",
            "billing_source": "go",
            "list_cost_usd": 0.02,
            "allowance_usd": 0.006,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("final_priced", [True, False])
async def test_repaired_request_adds_billing_once_and_retains_final_usage(
    final_priced: bool,
) -> None:
    model, url = WIRES["responses"]
    adapter = go_adapter()
    failed = {
        "status": "failed",
        "error": {
            "code": "invalid_request_error",
            "message": "Invalid max_tokens value, the valid range of max_tokens is [1, 4000]",
        },
        "usage": {
            "cost": 0,
            "opencode": {"billing_source": "go", "allowance": 0.01, "list_cost": 0.1},
        },
    }
    final = success_response("responses", streaming=False).json()
    final["usage"].update(
        {"cost": 0.25, "opencode": {"billing_source": "balance", "list_cost": 0.25}}
    )
    if not final_priced:
        final["usage"].pop("cost")
        final["usage"]["opencode"]["billing_source"] = "byok"
    with respx.mock:
        route = respx.post(url).mock(
            side_effect=[
                httpx.Response(
                    200, text=f"event: response.{name}\ndata: {json.dumps({'response': body})}\n\n"
                )
                for name, body in (("failed", failed), ("completed", final))
            ]
        )
        accumulator = StreamingAccumulator()
        async for delta in adapter.stream(HELLO, model_id=model, max_output_tokens=8000):
            accumulator.add_delta(delta)
    await adapter.aclose()
    observed = accumulator.finalize_assistant_fields().usage
    assert route.call_count == 2
    expected_amount = (
        {"source": "provider", "amount_usd": 0.25}
        if final_priced
        else {
            "source": "unknown",
            "amount_usd": None,
            "reason": "missing_price",
        }
    )
    assert observed and observed.get("reported_cost_usd") == expected_amount["amount_usd"]
    assert observed["cost"] == {
        **expected_amount,
        "allowance_usd": 0.01,
        "list_cost_usd": 0.35,
        "billing_source": "mixed",
    }
    assert observed["last_request_usage"]["cost"] == {
        **expected_amount,
        "list_cost_usd": 0.25,
        "billing_source": "balance" if final_priced else "byok",
    }
