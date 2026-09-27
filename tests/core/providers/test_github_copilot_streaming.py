"""GitHub Copilot stream transport: SSE framing, connect retry, and stream failures."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.providers.adapter import TERMINAL_OUTCOME_STOP
from core.providers.errors import NetworkError, ProviderError, ProviderTimeoutError
from core.utils.errors import VBotError
from tests.core.providers.github_copilot_test_support import (
    RESPONSES_URL,
    SAMPLE_MESSAGES,
    BrokenStream,
    RotatingTokenGetter,
    make_adapter,
    sse_events,
    sse_response,
)

_TEXT_DELTA = {"type": "content_delta", "text": "Hi"}
_RESPONSES_TEXT = sse_events({"type": "response.output_text.delta", "delta": "Hi"})
_RESPONSES_COMPLETED = sse_events(
    {
        "type": "response.completed",
        "response": {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
    }
)


@pytest.mark.asyncio
async def test_responses_stream_flushes_a_final_event_without_blank_line_terminator() -> None:
    # The gateway may close the stream right after the last event's data line.
    body = _RESPONSES_TEXT + _RESPONSES_COMPLETED.rstrip("\n")

    with respx.mock:
        respx.post(RESPONSES_URL).mock(return_value=sse_response(body))
        deltas = [
            delta async for delta in make_adapter().stream(SAMPLE_MESSAGES, model_id="gpt-5-mini")
        ]

    assert deltas == [
        _TEXT_DELTA,
        {"type": "usage", "input_tokens": 1, "output_tokens": 2},
        {"type": "finish", "reason": TERMINAL_OUTCOME_STOP},
    ]


@pytest.mark.asyncio
async def test_responses_stream_joins_split_crlf_and_keeps_raw_line_separators() -> None:
    # Node/Go gateways emit U+2028/U+2029/NEL unescaped inside JSON strings.
    text = "one two three\x85four"
    delta = json.dumps({"type": "response.output_text.delta", "delta": text}, ensure_ascii=False)
    completed = '{"type":"response.completed","response":{"status":"completed"}}'
    encoded = (
        f"event: response.output_text.delta\r\ndata: {delta}\r\n\r\ndata: {completed}\r\n\r\n"
    ).encode()
    crlf_split = encoded.index(b"\r\n\r\n") + 1  # CR and LF arrive in separate chunks

    async def chunks() -> AsyncIterator[bytes]:
        yield encoded[:crlf_split]
        yield encoded[crlf_split:]

    with respx.mock:
        respx.post(RESPONSES_URL).mock(return_value=sse_response(chunks()))
        deltas = [
            delta async for delta in make_adapter().stream(SAMPLE_MESSAGES, model_id="gpt-5-mini")
        ]

    assert deltas == [
        {"type": "content_delta", "text": text},
        {"type": "finish", "reason": TERMINAL_OUTCOME_STOP},
    ]


@pytest.mark.asyncio
async def test_stream_connect_retry_rebuilds_auth_headers() -> None:
    token_getter = RotatingTokenGetter(["stale-token", "fresh-token"])

    with respx.mock, patch("core.utils.retry._sleep", new_callable=AsyncMock):
        route = respx.post(RESPONSES_URL).mock(
            side_effect=[
                httpx.Response(503, text="Service Unavailable"),
                sse_response(_RESPONSES_TEXT + _RESPONSES_COMPLETED),
            ]
        )
        adapter = make_adapter(token_getter=token_getter)
        async for _ in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
            pass

    assert [call.request.headers["authorization"] for call in route.calls] == [
        "Bearer stale-token",
        "Bearer fresh-token",
    ]


_ROUTES = {
    "responses": ("gpt-5-mini", _RESPONSES_TEXT),
    "messages": (
        "claude-sonnet-4.6",
        sse_events(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hi"},
            }
        ),
    ),
}
_FAILURES: dict[str, tuple[Callable[[str], str | BrokenStream], type[VBotError]]] = {
    "eof-without-terminal-event": (lambda frame: frame, NetworkError),
    "mid-stream-read-error": (
        lambda frame: BrokenStream(frame, httpx.ReadError("connection reset")),
        NetworkError,
    ),
    "mid-stream-timeout": (
        lambda frame: BrokenStream(frame, httpx.ReadTimeout("timed out")),
        ProviderTimeoutError,
    ),
    "malformed-json": (lambda frame: frame + "data: not-json\n\n", ProviderError),
    "non-object-json": (lambda frame: frame + "data: [1]\n\n", ProviderError),
}


@pytest.mark.parametrize(
    ("route", "failure"),
    [
        pytest.param(route, failure, id=f"{route}-{failure}")
        for route in _ROUTES
        for failure in _FAILURES
        # The Responses decoder rejects non-object JSON itself (decoding tests).
        if (route, failure) != ("responses", "non-object-json")
    ],
)
@pytest.mark.asyncio
async def test_stream_failure_after_first_delta_raises_and_closes_the_body(
    route: str, failure: str
) -> None:
    model_id, first_frame = _ROUTES[route]
    make_body, expected_error = _FAILURES[failure]
    body = make_body(first_frame)
    received: list[dict[str, object]] = []

    with respx.mock:
        respx.post(url__regex=r".*").mock(return_value=sse_response(body))
        with pytest.raises(VBotError) as caught:
            async for delta in make_adapter().stream(SAMPLE_MESSAGES, model_id=model_id):
                received.append(delta)

    assert type(caught.value) is expected_error
    assert received == [_TEXT_DELTA]
    if isinstance(body, BrokenStream):
        assert body.closed is True
