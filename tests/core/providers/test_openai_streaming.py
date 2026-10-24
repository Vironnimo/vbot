"""OpenAI Adapter streaming: Codex SSE deltas, send() over the stream, and reasoning summaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from compression import zstd
from typing import Any, cast, override
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

import core.providers.http as http_module
from core.chat.streaming import StreamingAccumulator, StreamingDeltaBatcher
from core.providers.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUsageLimitError,
)
from core.providers.openai import RESPONSES_ZSTD_MIN_BYTES, OpenAIAdapter, ResponsesTransports
from core.utils.retry import caller_owns_retries
from tests.core.chat.assistant_turn_test_support import (
    assistant_turn_from_response,
    event_payload,
    request_history,
)

from .openai_test_support import (
    CHAT_COMPLETIONS_URL,
    COMPLETED_RESPONSE,
    OPENAI_SUBSCRIPTION_URL,
    PLATFORM_RESPONSES_URL,
    SAMPLE_MESSAGES,
    RotatingTokenGetter,
    codex_adapter,
    codex_sse,
    jwt_with_account,
    platform_adapter,
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

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._stall:
            await asyncio.Event().wait()

    @override
    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_codex_send_times_out_and_closes_the_response_when_its_stream_stalls() -> None:
    """Non-streaming send() semantics bound the internally streamed Codex wire."""

    body = _TrackedStream(stall=True)

    # The wait also covers preparing and connecting the request, which can take
    # several milliseconds on a loaded test worker.
    with (
        respx.mock,
        patch("core.providers.openai.PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS", 0.2),
    ):
        respx.post(OPENAI_SUBSCRIPTION_URL).mock(return_value=httpx.Response(200, stream=body))
        with pytest.raises(ProviderTimeoutError):
            await codex_adapter().send(SAMPLE_MESSAGES, model_id="gpt-5.5")

    assert body.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503])
async def test_codex_stream_connect_retry_rebuilds_account_headers(status: int) -> None:
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
            side_effect=[httpx.Response(status, text="temporarily unavailable"), sse_response(body)]
        )
        await _collect(adapter)

    assert [call.request.headers["chatgpt-account-id"] for call in route.calls] == [
        "acct-stale",
        "acct-fresh",
    ]


@pytest.mark.parametrize(
    ("subscription", "model_id", "url", "compressed", "websocket"),
    [
        (True, MODEL_ID, OPENAI_SUBSCRIPTION_URL, False, False),
        (True, MODEL_ID, OPENAI_SUBSCRIPTION_URL, True, False),
        (True, MODEL_ID, OPENAI_SUBSCRIPTION_URL, False, True),
        (False, MODEL_ID, PLATFORM_RESPONSES_URL, False, False),
        (False, MODEL_ID, PLATFORM_RESPONSES_URL, True, False),
        (False, MODEL_ID, PLATFORM_RESPONSES_URL, False, True),
        (False, "gpt-4.1", CHAT_COMPLETIONS_URL, False, False),
    ],
    ids=[
        "codex",
        "codex-zstd",
        "codex-ws-fallback",
        "platform",
        "platform-zstd",
        "platform-ws-fallback",
        "chat-completions",
    ],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@pytest.mark.asyncio
async def test_http_500_retries_only_stream_establishment(
    subscription: bool, model_id: str, url: str, compressed: bool, websocket: bool, streaming: bool
) -> None:
    connector = AsyncMock(side_effect=OSError("test websocket unavailable"))
    factory = codex_adapter if subscription else platform_adapter
    adapter = factory(responses_websocket_connect=connector)
    context = {"conversation_id": "coder:session"} if websocket else {}
    messages = _user_messages("A" * RESPONSES_ZSTD_MIN_BYTES) if compressed else SAMPLE_MESSAGES
    try:
        with respx.mock, caller_owns_retries():
            route = respx.post(url).mock(
                return_value=httpx.Response(
                    500, text="test-server-error", headers={"Retry-After": "2"}
                )
            )
            with pytest.raises(ProviderError) as caught:
                if streaming:
                    _ = [
                        chunk
                        async for chunk in adapter.stream(messages, model_id=model_id, **context)
                    ]
                else:
                    await adapter.send(messages, model_id=model_id, **context)
        assert caught.value.status_code == 500
        assert caught.value.retryable is streaming
        assert caught.value.retry_after == (2.0 if streaming else None)
        assert route.call_count == 1
        assert bool(connector.await_count) is (websocket and (streaming or subscription))
        assert (route.calls[0].request.headers.get("Content-Encoding") == "zstd") is compressed
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("error", "headers", "expected_type", "retry_after", "wording"),
    [
        pytest.param(
            # Reset 2100-01-01T00:00:00Z: far beyond any retry wait.
            {"type": "usage_limit_reached", "plan_type": "plus", "resets_at": 4_102_444_800},
            {"retry-after": "20"},
            ProviderUsageLimitError,
            None,
            "Subscription usage limit reached; it resets at 2100-01-01 01:00 CET: 429 {",
            id="distant-usage-limit-reset-stops",
        ),
        pytest.param(
            {"type": "usage_limit_reached", "plan_type": "plus"},
            {"retry-after": "20"},
            ProviderRateLimitError,
            20.0,
            ", in 20 seconds: 429 {",
            id="usage-limit-reset-from-retry-after-waits-exactly",
        ),
        pytest.param(
            {"type": "usage_not_included"},
            {},
            ProviderError,
            None,
            "Quota exhausted: 429 {",
            id="exhausted-entitlement-stops",
        ),
        pytest.param(
            {"type": "test_rate_limit_type"},
            {"retry-after": "7"},
            ProviderRateLimitError,
            7.0,
            "Rate limited: 429 {",
            id="other-429-stays-a-rate-limit",
        ),
    ],
)
@pytest.mark.asyncio
async def test_codex_http_429_tells_an_exhausted_allowance_from_a_rate_limit(
    error: dict[str, Any],
    headers: dict[str, str],
    expected_type: type[ProviderError],
    retry_after: float | None,
    wording: str,
) -> None:
    """The body's ``error`` decides; a usage limit's reset is named in the display timezone."""

    adapter = codex_adapter()
    adapter.display_timezone = ZoneInfo("Europe/Berlin")

    with respx.mock, caller_owns_retries():
        respx.post(OPENAI_SUBSCRIPTION_URL).mock(
            return_value=httpx.Response(429, json={"error": error}, headers=headers)
        )
        with pytest.raises(ProviderError) as caught:
            await _collect(adapter)

    assert type(caught.value) is expected_type
    assert caught.value.retryable is (expected_type is ProviderRateLimitError)
    assert caught.value.retry_after == retry_after
    assert caught.value.status_code == 429
    assert wording in str(caught.value)


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
        responses_websocket_connect=AsyncMock(side_effect=OSError("connection unavailable"))
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
# Request body compression
# ---------------------------------------------------------------------------

_COMPLETED_SSE = codex_sse(_completed({"id": "resp_1"}))


def _user_messages(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


async def _stream_text(adapter: OpenAIAdapter, text: str) -> None:
    async for _chunk in adapter.stream(_user_messages(text), model_id=MODEL_ID):
        pass


@pytest.mark.parametrize("wire", ["codex-stream", "platform-stream", "platform-send"])
@pytest.mark.parametrize("large", [True, False], ids=["large-zstd", "small-plain"])
@pytest.mark.asyncio
async def test_large_responses_http_bodies_are_zstd_compressed_once_for_every_attempt(
    wire: str, large: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex and the Platform (which documents zstd for ``POST /v1/responses``) both
    get a large body compressed, streaming or not."""

    compressions: list[None] = []
    compress = http_module._zstd_compress

    def counting_compress(body: bytes) -> bytes:
        compressions.append(None)
        return compress(body)

    monkeypatch.setattr(http_module, "_zstd_compress", counting_compress)
    text = "x" * (RESPONSES_ZSTD_MIN_BYTES if large else 100)
    adapter = codex_adapter() if wire == "codex-stream" else platform_adapter()
    url = OPENAI_SUBSCRIPTION_URL if wire == "codex-stream" else PLATFORM_RESPONSES_URL
    completed = (
        httpx.Response(200, json=COMPLETED_RESPONSE)
        if wire == "platform-send"
        else sse_response(_COMPLETED_SSE)
    )

    with respx.mock:
        route = respx.post(url).mock(
            side_effect=[httpx.Response(503, text="Service Unavailable"), completed]
        )
        if wire == "platform-send":
            await adapter.send(_user_messages(text), model_id=MODEL_ID)
        else:
            await _stream_text(adapter, text)

    first, retried = (call.request for call in route.calls)
    assert retried.content == first.content
    if large:
        assert first.headers["content-encoding"] == "zstd"
        payload = json.loads(zstd.decompress(first.content))
    else:
        assert "content-encoding" not in first.headers
        payload = json.loads(first.content)
    assert compressions == ([None] if large else [])
    assert payload["input"][-1]["content"] == [{"type": "input_text", "text": text}]
    await adapter.aclose()


@pytest.mark.parametrize(
    "rejection",
    [
        pytest.param(httpx.Response(415, text="Unsupported Media Type"), id="415"),
        pytest.param(
            httpx.Response(400, json={"error": {"message": "Unsupported Content-Encoding: zstd"}}),
            id="400-naming-the-encoding",
        ),
        pytest.param(
            httpx.Response(
                400, text="Bad request", headers={"x-openai-retry-uncompressed": "true"}
            ),
            id="retry-uncompressed-header",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_rejected_zstd_body_is_resent_uncompressed_and_the_connection_stops_compressing(
    rejection: httpx.Response,
) -> None:
    """The rejection is remembered for the Connection, so a later Run sends plain JSON."""

    transports = ResponsesTransports()
    text = "x" * RESPONSES_ZSTD_MIN_BYTES

    with respx.mock:
        route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
            side_effect=[rejection, sse_response(_COMPLETED_SSE), sse_response(_COMPLETED_SSE)]
        )
        for _run in range(2):
            adapter = codex_adapter(responses_transports=transports)
            await _stream_text(adapter, text)
            await adapter.aclose()

    rejected, resent, later = (call.request for call in route.calls)
    assert rejected.headers["content-encoding"] == "zstd"
    assert "content-encoding" not in resent.headers
    assert "content-encoding" not in later.headers
    assert json.loads(resent.content) == json.loads(zstd.decompress(rejected.content))


@pytest.mark.asyncio
async def test_another_error_of_a_zstd_request_stays_that_error() -> None:
    transports = ResponsesTransports()
    adapter = codex_adapter(responses_transports=transports)

    with respx.mock:
        route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
            return_value=httpx.Response(400, json={"error": {"message": "Invalid input."}})
        )
        with pytest.raises(ProviderError, match="Invalid input"):
            await _stream_text(adapter, "x" * RESPONSES_ZSTD_MIN_BYTES)

    assert route.call_count == 1
    assert transports.compresses_requests(("openai", "subscription"))
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
