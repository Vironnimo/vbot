"""OpenAI Adapter streaming: Codex SSE deltas, send() over the stream, and reasoning summaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.chat.streaming import StreamingAccumulator, StreamingDeltaBatcher
from core.providers.errors import ProviderTimeoutError
from core.providers.openai import OpenAIAdapter
from tests.core.chat.assistant_turn_test_support import (
    assistant_turn_from_response,
    event_payload,
    request_history,
)

from .openai_test_support import (
    OPENAI_SUBSCRIPTION_URL,
    SAMPLE_MESSAGES,
    RotatingTokenGetter,
    codex_adapter,
    codex_sse,
    jwt_with_account,
    sse_response,
)

MODEL_ID = "gpt-5.6-terra"


async def _collect(adapter: OpenAIAdapter, **kwargs: Any) -> list[dict[str, Any]]:
    return [chunk async for chunk in adapter.stream(SAMPLE_MESSAGES, model_id=MODEL_ID, **kwargs)]


async def stream_chunks(*events: dict[str, Any] | str) -> list[dict[str, Any]]:
    """Stream mocked Codex SSE events through the subscription Adapter."""

    with respx.mock:
        respx.post(OPENAI_SUBSCRIPTION_URL).mock(return_value=sse_response(codex_sse(*events)))
        return await _collect(codex_adapter())


def _completed(response: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.completed", "response": {"status": "completed", **response}}


# ---------------------------------------------------------------------------
# Codex SSE stream and send() over one streaming exchange
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_stream_yields_normalized_responses_deltas() -> None:
    """The SSE ``event:`` name stands in for an event whose data carries no ``type``."""

    chunks = await stream_chunks(
        'event: response.output_text.delta\ndata: {"delta":"Hel"}\n\n',
        _completed({"id": "resp_1", "usage": {"input_tokens": 1, "output_tokens": 2}}),
    )

    assert chunks == [
        {"type": "content_delta", "text": "Hel"},
        {"type": "reasoning_meta", "reasoning_meta": {"response_id": "resp_1"}},
        {"type": "usage", "input_tokens": 1, "output_tokens": 2},
        {"type": "finish", "reason": "stop"},
    ]


@pytest.mark.asyncio
async def test_codex_send_accumulates_streamed_text_when_completed_output_is_empty() -> None:
    """The live Codex wire may leave completed.output empty after streaming text."""

    adapter = codex_adapter()
    body = codex_sse(
        {"type": "response.output_text.delta", "delta": "Generated "},
        {"type": "response.output_text.delta", "delta": "title"},
        _completed(
            {"id": "resp_1", "output": [], "usage": {"input_tokens": 2, "output_tokens": 2}}
        ),
    )

    with respx.mock:
        route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(return_value=sse_response(body))
        response = await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5")

    assert route.call_count == 1
    assert adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Generated title",
        "reasoning": None,
        "reasoning_meta": {"response_id": "resp_1"},
        "tool_calls": None,
        "usage": {"input_tokens": 2, "output_tokens": 2},
        "terminal_outcome": "stop",
    }


class _TrackedStream(httpx.AsyncByteStream):
    """A response body that yields ``chunks`` and then optionally stalls."""

    def __init__(self, *chunks: bytes, stall: bool = False) -> None:
        self._chunks = chunks
        self._stall = stall
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._stall:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_codex_send_times_out_and_closes_the_response_when_its_stream_stalls() -> None:
    """Non-streaming send() semantics bound the internally streamed Codex wire."""

    body = _TrackedStream(stall=True)

    with (
        respx.mock,
        patch("core.providers.openai.PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS", 0.01),
    ):
        respx.post(OPENAI_SUBSCRIPTION_URL).mock(return_value=httpx.Response(200, stream=body))
        with pytest.raises(ProviderTimeoutError):
            await codex_adapter().send(SAMPLE_MESSAGES, model_id="gpt-5.5")

    assert body.closed is True


@pytest.mark.asyncio
async def test_codex_stream_connect_retry_rebuilds_account_headers() -> None:
    """A retried Codex stream connect re-consults the token getter (OAuth refresh)."""

    adapter = codex_adapter(
        RotatingTokenGetter([jwt_with_account("acct-stale"), jwt_with_account("acct-fresh")])
    )
    body = codex_sse(
        {"type": "response.output_text.delta", "delta": "Hi"},
        _completed({"id": "resp_1"}),
    )

    with respx.mock:
        route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
            side_effect=[httpx.Response(503, text="Service Unavailable"), sse_response(body)]
        )
        await _collect(adapter)

    assert [call.request.headers["chatgpt-account-id"] for call in route.calls] == [
        "acct-stale",
        "acct-fresh",
    ]


@pytest.mark.parametrize(
    "conversation_id",
    [pytest.param(None, id="sse"), pytest.param("agent:session", id="websocket-fallback")],
)
@pytest.mark.asyncio
async def test_closing_a_partial_codex_sse_stream_closes_the_response(
    conversation_id: str | None,
) -> None:
    body = _TrackedStream(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
    adapter = codex_adapter(
        codex_websocket_connect=AsyncMock(side_effect=OSError("connection unavailable"))
    )
    stream = cast(
        AsyncGenerator[dict[str, Any]],
        adapter.stream(SAMPLE_MESSAGES, model_id=MODEL_ID, conversation_id=conversation_id),
    )

    with respx.mock:
        respx.post(OPENAI_SUBSCRIPTION_URL).mock(return_value=httpx.Response(200, stream=body))
        try:
            assert await anext(stream) == {"type": "content_delta", "text": "partial"}
            await stream.aclose()
            assert body.closed is True
        finally:
            await stream.aclose()
            await adapter.aclose()


# ---------------------------------------------------------------------------
# Reasoning summary sections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_summary_sections_stream_backfill_and_stay_out_of_replay() -> None:
    """Visible summary structure survives streaming without exposing replay state."""

    sections = [
        "**Inspecting files**\n\nRead the source.",
        "**Comparing options**\n\nChoose a fix.",
    ]
    events: list[dict[str, Any]] = []
    for index, section in enumerate(sections):
        position = {"output_index": 0, "summary_index": index}
        events.append(
            {
                "type": "response.reasoning_summary_part.added",
                **position,
                "part": {"type": "summary_text", "text": ""},
            }
        )
        events.extend(
            {"type": "response.reasoning_summary_text.delta", **position, "delta": fragment}
            for fragment in (section[:8], section[8:])
        )
        events.append({"type": "response.reasoning_summary_text.done", **position, "text": section})
    item = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text} for text in sections],
        "encrypted_content": "opaque-sentinel",
    }
    events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
    events.append(
        _completed(
            {
                "output": [
                    item,
                    {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "Result"}],
                    },
                ]
            }
        )
    )
    adapter = codex_adapter()

    with respx.mock:
        respx.post(OPENAI_SUBSCRIPTION_URL).mock(
            side_effect=lambda _request: sse_response(codex_sse(*events))
        )
        chunks = await _collect(adapter)
        sent = adapter.normalize_response(await adapter.send(SAMPLE_MESSAGES, model_id=MODEL_ID))

    accumulator = StreamingAccumulator()
    visible = [shown for chunk in chunks for shown in accumulator.add_delta(chunk)]
    fields = accumulator.finalize_assistant_fields()
    assert fields.reasoning_summary == sections
    assert fields.reasoning == "\n\n".join(sections)
    assert not accumulator.ends_with_reasoning
    assert sent["reasoning_summary"] == sections
    assert adapter.normalize_response({"output": [item]})["reasoning_summary"] == sections

    message = assistant_turn_from_response("test/model", fields.to_response_dict())
    public = event_payload(message)
    assert public["reasoning_summary"] == sections
    assert "opaque-sentinel" not in str(public)
    [replay] = request_history([], current_turn=message)
    assert "reasoning_summary" not in replay
    assert replay["reasoning_meta"]["response_output"][0] == item

    batcher = StreamingDeltaBatcher(interval_seconds=100)
    batches = []
    batcher.add(visible[-1])  # Start the cadence before collecting the summary fragments.
    for delta in visible:
        batches.extend(batcher.add(delta))
    batches.extend(batcher.flush())
    summary_events = [event for event in batches if "summary_index" in event.payload]
    assert [event.payload["summary_index"] for event in summary_events] == [0, 1]
    assert [event.payload["summary_text"] for event in summary_events] == sections


@pytest.mark.asyncio
async def test_terminal_only_reasoning_summaries_keep_item_boundaries() -> None:
    output = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": text}]}
        for text in ["One", "Two"]
    ]

    accumulator = StreamingAccumulator()
    for chunk in await stream_chunks(_completed({"output": output})):
        accumulator.add_delta(chunk)

    assert accumulator.finalize_partial_fields().reasoning_summary == ["One", "Two"]
    assert accumulator.partial_reasoning == "One\n\nTwo"
