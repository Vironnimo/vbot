"""Shared Provider HTTP transport: timeouts, error classification, establishment retry,
stream line framing and the debug capture of the exact wire exchange."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import zlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, override
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from core.debug.recorder import DebugContext, ProviderDebugRecorder
from core.debug.store import DebugTraceStore
from core.providers._http_shared import (
    PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS,
    build_async_client,
    build_streaming_request,
    classify_http_status,
    connect_streaming_with_retry,
    decode_response_json,
    execute_with_sampling_fallback,
    format_http_error_detail,
    iter_sse_events,
    iter_stream_lines,
    parse_sse_json_data,
    post_json_with_retry,
    provider_chat_timeout,
    provider_streaming_timeout,
    split_stream_lines,
    wrap_network_error,
)
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequestTooLargeError,
    ProviderTimeoutError,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.utils.retry import caller_owns_retries

from .adapter_test_support import TOKEN, bearer_config


@pytest.mark.asyncio
async def test_non_streaming_reads_are_bounded_and_stream_reads_are_left_to_chat_clocks() -> None:
    chat = provider_chat_timeout()
    streaming = provider_streaming_timeout()
    client = build_async_client(base_url="https://example.com")
    try:
        non_streaming_request = client.build_request("POST", "/response")
        streaming_request = build_streaming_request(client, "POST", "/stream")
    finally:
        await client.aclose()

    assert (chat.connect, chat.read, chat.write, chat.pool) == (
        60.0,
        PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS,
        60.0,
        60.0,
    )
    assert (streaming.connect, streaming.read, streaming.write, streaming.pool) == (
        60.0,
        None,
        60.0,
        60.0,
    )
    assert non_streaming_request.extensions["timeout"]["read"] == (
        PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS
    )
    assert streaming_request.extensions["timeout"]["read"] is None


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected_type", "detail"),
    [
        (httpx.ReadTimeout("read timed out"), ProviderTimeoutError, "read timed out"),
        (httpx.ConnectTimeout(""), ProviderTimeoutError, "ConnectTimeout"),
        (httpx.ConnectError("connection refused"), NetworkError, "connection refused"),
    ],
    ids=["timeout", "empty-timeout-names-its-type", "transport-failure"],
)
def test_network_failures_stay_retryable_and_only_timeouts_are_provider_errors(
    error: httpx.TransportError, expected_type: type[Exception], detail: str
) -> None:
    wrapped = wrap_network_error(error)

    assert type(wrapped) is expected_type
    assert wrapped.retryable is True
    # A NetworkError is not a Provider failure and never advances a Model fallback.
    assert isinstance(wrapped, ProviderError) is (expected_type is ProviderTimeoutError)
    assert detail in str(wrapped)


def _json_response(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, request=httpx.Request("POST", "https://example.com/"))


def test_valid_json_decodes_from_stream_data_and_response_bodies() -> None:
    assert parse_sse_json_data('{"id":"1"}', context="Stub provider") == {"id": "1"}
    assert decode_response_json(_json_response(b'{"id":"1"}'), "Stub provider") == {"id": "1"}


@pytest.mark.parametrize(
    ("decode", "cause"),
    [
        (lambda: parse_sse_json_data('{"id":\n', context="Stub provider"), json.JSONDecodeError),
        (
            lambda: decode_response_json(_json_response(b'{"id":\n'), "Stub provider"),
            json.JSONDecodeError,
        ),
        (lambda: decode_response_json(_json_response(b"[1, 2]"), "Stub provider"), type(None)),
    ],
    ids=["malformed-stream-data", "malformed-response", "non-object-response"],
)
def test_unusable_provider_json_is_a_fatal_error_naming_the_provider(
    decode: Callable[[], Any], cause: type
) -> None:
    with pytest.raises(ProviderError) as raised:
        decode()

    assert raised.value.retryable is False
    assert "Stub provider" in str(raised.value)
    assert type(raised.value.__cause__) is cause


@pytest.mark.parametrize(
    ("status", "idempotent", "headers", "detail", "expected_type", "retryable", "retry_after"),
    [
        (429, False, {"Retry-After": "7"}, "429 slow down", ProviderRateLimitError, True, 7.0),
        (429, False, None, "", ProviderRateLimitError, True, None),
        (503, False, {"retry-after-ms": "2000"}, "", ProviderError, True, 2.0),
        (500, False, None, "500 boom", ProviderError, False, None),
        (500, True, None, "", ProviderError, True, None),
        (400, False, {"Retry-After": "9"}, "400 bad request", ProviderError, False, None),
        (401, False, {"Retry-After": "9"}, "401 bad key", ProviderAuthError, False, None),
        # Chat retires delivered images and retries smaller after a body-size rejection.
        (413, False, None, "413 body too large", ProviderRequestTooLargeError, False, None),
    ],
    ids=[
        "rate-limit-retry-after",
        "rate-limit-without-headers",
        "retryable-retry-after-ms",
        "server-error-not-replay-safe",
        "server-error-replay-safe",
        "fatal-ignores-hint",
        "auth-ignores-hint",
        "request-too-large",
    ],
)
def test_error_status_raises_the_classified_error_with_status_detail_and_hint(
    status: int,
    idempotent: bool,
    headers: dict[str, str] | None,
    detail: str,
    expected_type: type[ProviderError],
    retryable: bool,
    retry_after: float | None,
) -> None:
    with pytest.raises(ProviderError) as raised:
        classify_http_status(
            status,
            idempotent=idempotent,
            detail=detail,
            response_headers=httpx.Headers(headers) if headers is not None else None,
        )

    error = raised.value
    assert type(error) is expected_type
    assert (error.retryable, error.retry_after, error.status_code) == (
        retryable,
        retry_after,
        status,
    )
    assert (detail or str(status)) in str(error)


def test_error_detail_is_status_and_body_or_the_bare_status() -> None:
    assert format_http_error_detail(502, "gateway boom") == "502 gateway boom"
    assert format_http_error_detail(502, "") == "502"
    assert format_http_error_detail(502, None) == "502"


# ---------------------------------------------------------------------------
# Sampling-parameter fallback
# ---------------------------------------------------------------------------

_SAMPLED_PAYLOAD = {"model": "m", "temperature": 0.7, "top_p": 0.9, "top_k": 40}


@pytest.mark.parametrize(
    ("detail", "blamed"),
    [
        ("Provider error: 400 Unsupported parameter: 'temperature'", "temperature"),
        ("Provider error: 400 Unknown parameter: top_k", "top_k"),
    ],
)
@pytest.mark.asyncio
async def test_sampling_rejection_strips_the_blamed_parameter_and_retries_once(
    detail: str, blamed: str
) -> None:
    payload = dict(_SAMPLED_PAYLOAD)
    attempts: list[dict[str, Any]] = []

    async def attempt() -> str:
        attempts.append(dict(payload))
        if len(attempts) == 1:
            raise ProviderError(detail, retryable=False)
        return "ok"

    result = await execute_with_sampling_fallback(
        attempt, payload, logger=logging.getLogger("test"), provider_label="stub"
    )

    assert result == "ok"
    assert attempts == [
        _SAMPLED_PAYLOAD,
        {key: value for key, value in _SAMPLED_PAYLOAD.items() if key != blamed},
    ]


@pytest.mark.parametrize(
    "error",
    [
        ProviderError("400 Unsupported parameter: 'temperature'", retryable=False),
        ProviderError("400 Unsupported parameter: 'max_tokens'", retryable=False),
        ProviderError("400 top_p", retryable=False),
        ProviderAuthError("Unsupported parameter top_p for this credential"),
        ProviderRateLimitError("Unsupported parameter top_p: backend overloaded"),
    ],
    ids=[
        "parameter-not-sent",
        "not-a-sampling-parameter",
        "no-rejection-marker",
        "auth-failure",
        "retryable-failure",
    ],
)
@pytest.mark.asyncio
async def test_other_failures_pass_through_the_sampling_fallback_unchanged(
    error: ProviderError,
) -> None:
    payload = {"model": "m", "top_p": 0.9}
    attempt = AsyncMock(side_effect=error)

    with pytest.raises(ProviderError) as raised:
        await execute_with_sampling_fallback(
            attempt, payload, logger=logging.getLogger("test"), provider_label="stub"
        )

    assert raised.value is error
    attempt.assert_awaited_once()
    assert payload == {"model": "m", "top_p": 0.9}


# ---------------------------------------------------------------------------
# connect_streaming_with_retry / post_json_with_retry — request establishment
# ---------------------------------------------------------------------------

_Establish = Callable[..., Awaitable[Any]]


async def _establish_stream(client: httpx.AsyncClient, **kwargs: Any) -> Any:
    response = await connect_streaming_with_retry(client, "/v1/chat", {"model": "m"}, **kwargs)
    try:
        return json.loads(await response.aread())
    finally:
        await response.aclose()


async def _post_json(client: httpx.AsyncClient, **kwargs: Any) -> Any:
    return await post_json_with_retry(
        client, "/v1/chat", {"model": "m"}, provider_context="Stub provider", **kwargs
    )


_ESTABLISHERS = pytest.mark.parametrize(
    "establish", [_establish_stream, _post_json], ids=["streaming", "json"]
)


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="https://example.com", transport=httpx.MockTransport(handler))


def _never_expected_error_status(status_code: int, error_body: str, headers: httpx.Headers) -> None:
    raise AssertionError(f"error handler unexpectedly called for {status_code}: {error_body}")


async def _no_headers() -> dict[str, str]:
    return {}


@_ESTABLISHERS
@pytest.mark.asyncio
async def test_establishment_retries_with_headers_rebuilt_for_every_attempt(
    establish: _Establish,
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, content=b'{"ok": true}')

    async def build_headers() -> dict[str, str]:
        return {"Authorization": f"Bearer t{len(sent) + 1}"}

    def handle_error_status(status_code: int, error_body: str, headers: httpx.Headers) -> None:
        classify_http_status(
            status_code,
            idempotent=False,
            detail=format_http_error_detail(status_code, error_body),
            response_headers=headers,
        )

    client = _mock_client(handler)
    try:
        result = await establish(
            client, build_headers=build_headers, handle_error_status=handle_error_status
        )
    finally:
        await client.aclose()

    assert result == {"ok": True}
    assert [request.headers["Authorization"] for request in sent] == ["Bearer t1", "Bearer t2"]
    assert [request.url.path for request in sent] == ["/v1/chat", "/v1/chat"]
    assert all(json.loads(request.content) == {"model": "m"} for request in sent)


@_ESTABLISHERS
@pytest.mark.asyncio
async def test_error_status_handler_receives_the_read_error_body(
    establish: _Establish,
) -> None:
    calls: list[tuple[int, str]] = []

    def handle_error_status(status_code: int, error_body: str, headers: httpx.Headers) -> None:
        calls.append((status_code, error_body))
        raise ProviderAuthError(f"Authentication error: {error_body}")

    client = _mock_client(lambda request: httpx.Response(401, content=b"bad key"))
    try:
        with pytest.raises(ProviderAuthError):
            await establish(
                client, build_headers=_no_headers, handle_error_status=handle_error_status
            )
    finally:
        await client.aclose()

    assert calls == [(401, "bad key")]


@_ESTABLISHERS
@pytest.mark.asyncio
async def test_adapter_transport_wrapper_relabels_connection_failures(
    establish: _Establish,
) -> None:
    class ProviderOfflineError(Exception):
        pass

    wrapped: list[httpx.TransportError] = []

    def wrap_transport_error(error: httpx.TransportError) -> Exception:
        wrapped.append(error)
        return ProviderOfflineError("provider offline")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = _mock_client(refuse)
    try:
        with pytest.raises(ProviderOfflineError):
            await establish(
                client,
                build_headers=_no_headers,
                handle_error_status=_never_expected_error_status,
                wrap_transport_error=wrap_transport_error,
            )
    finally:
        await client.aclose()

    assert [type(error) for error in wrapped] == [httpx.ConnectError]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.ReadError, NetworkError),
        (httpx.ReadTimeout, ProviderTimeoutError),
        (asyncio.CancelledError, asyncio.CancelledError),
    ],
)
@pytest.mark.asyncio
async def test_failed_error_body_read_closes_the_stream(
    failure: type[BaseException], expected: type[BaseException]
) -> None:
    class BrokenBody(httpx.AsyncByteStream):
        closed = False

        @override
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield b"partial error"
            raise failure("body interrupted")

        @override
        async def aclose(self) -> None:
            self.closed = True

    body = BrokenBody()
    client = _mock_client(lambda request: httpx.Response(503, stream=body))
    try:
        with caller_owns_retries(), pytest.raises(expected):
            await connect_streaming_with_retry(
                client,
                "/stream",
                {},
                build_headers=_no_headers,
                handle_error_status=_never_expected_error_status,
            )
    finally:
        await client.aclose()

    assert body.closed


# ---------------------------------------------------------------------------
# Stream line framing
# ---------------------------------------------------------------------------


def _chunked_response(*chunks: bytes) -> httpx.Response:
    async def body():  # type: ignore[no-untyped-def]
        for chunk in chunks:
            yield chunk

    return httpx.Response(200, content=body())


# JSON permits these unescaped inside strings; httpx's line decoder and
# ``str.splitlines`` treat them as line breaks.
_UNICODE_SEPARATORS = "  \x85\x0b\x0c\x1c\x1d\x1e"


def test_split_stream_lines_breaks_only_at_cr_lf_and_crlf() -> None:
    text = f"a{_UNICODE_SEPARATORS}b\r\nc\rd\n\ne"

    assert split_stream_lines(text) == [f"a{_UNICODE_SEPARATORS}b", "c", "d", "", "e"]
    assert split_stream_lines("x\n") == ["x"]
    assert split_stream_lines("") == []


@pytest.mark.asyncio
async def test_iter_stream_lines_joins_crlf_and_utf8_split_across_chunks() -> None:
    encoded = f'data: {{"text":"a{_UNICODE_SEPARATORS}b"}}'.encode()
    split_at = encoded.index(" ".encode()) + 1  # inside the 3-byte sequence
    response = _chunked_response(
        encoded[:split_at], encoded[split_at:] + b"\r", b"\n\r", b"\ndata: 2\n"
    )

    lines = [line async for line in iter_stream_lines(response)]

    assert lines == [f'data: {{"text":"a{_UNICODE_SEPARATORS}b"}}', "", "data: 2"]


@pytest.mark.asyncio
async def test_iter_sse_events_keeps_unicode_line_separators_inside_json_data() -> None:
    payload = json.dumps({"delta": f"one{_UNICODE_SEPARATORS}two"}, ensure_ascii=False)
    response = _chunked_response(
        f"data: {payload}\r".encode(), b"\n\r\n", b": keepalive\n", b"data: [DONE]\n\n"
    )

    events = [event async for event in iter_sse_events(response)]

    assert [event.comment for event in events] == [None, "keepalive", None]
    assert parse_sse_json_data(events[0].data or "", context="test") == {
        "delta": f"one{_UNICODE_SEPARATORS}two"
    }
    assert events[2].data == "[DONE]"


# ---------------------------------------------------------------------------
# Debug capture of the wire exchange (canonical trace: .vorch/domain-maps/debug.md)
# ---------------------------------------------------------------------------

_DEBUG_URL = "https://debug.example.test/v1/chat/completions"
_REDACTED = "[REDACTED]"
_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"},
]
_COMPLETION = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
    return json.dumps(
        {
            "id": "chatcmpl-123",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        },
        separators=(",", ":"),
    )


_SSE_FRAMES = f"data: {_chunk({'content': 'Hello'})}\n\ndata: {_chunk({'content': ' world'})}\n\n"
_SSE_BODY = f"{_SSE_FRAMES}data: {_chunk({}, 'stop')}\n\ndata: [DONE]\n\n"


def _debug_context(*, streaming: bool) -> DebugContext:
    return DebugContext(
        run_id="run-debug-1",
        agent_id="agent-1",
        session_id="session-1",
        provider_id="debug",
        connection_id="debug:api-key",
        model_id="gpt-5.2",
        streaming=streaming,
        iteration_number=1,
    )


@pytest.fixture
def debug_store(tmp_path: Path) -> DebugTraceStore:
    return DebugTraceStore(tmp_path, trace_limit=50)


@pytest.fixture
def debug_adapter(debug_store: DebugTraceStore) -> OpenAICompatibleAdapter:
    """An Adapter built with a recorder, as the Runtime builds it with Debug Mode enabled."""

    return OpenAICompatibleAdapter(
        bearer_config(
            "debug", adapter="openai_compatible", extra_headers={"X-Custom-Header": "test-value"}
        ),
        TOKEN,
        debug_recorder=ProviderDebugRecorder(debug_store),
    )


def _traces(store: DebugTraceStore) -> list[dict[str, Any]]:
    return [store.get_trace(entry["trace_id"]) for entry in store.get_traces()]


async def _drain(adapter: OpenAICompatibleAdapter) -> list[dict[str, Any]]:
    return [delta async for delta in adapter.stream(_MESSAGES, model_id="gpt-5.2")]


def _sse(content: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        200, content=content, headers={"Content-Type": "text/event-stream", **(headers or {})}
    )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_encoding", "encode"),
    [
        (None, lambda body: body),
        ("gzip", gzip.compress),
        ("deflate, gzip", lambda body: gzip.compress(zlib.compress(body))),
    ],
)
async def test_send_persists_one_redacted_trace_of_the_exact_wire_exchange(
    debug_adapter: OpenAICompatibleAdapter,
    debug_store: DebugTraceStore,
    content_encoding: str | None,
    encode: Callable[[bytes], bytes],
) -> None:
    context = _debug_context(streaming=False)
    encoding_header = {"Content-Encoding": content_encoding} if content_encoding else {}
    route = respx.post(_DEBUG_URL).mock(
        return_value=httpx.Response(
            200,
            content=encode(json.dumps(_COMPLETION).encode()),
            headers={
                "Content-Type": "application/json",
                **encoding_header,
                "X-Request-Id": "req-001",
                "X-Debug-Secret": "do-not-leak",
                "X-Refresh-Token": "refresh-tkn-xxx",
                "X-Api-Key": "api-key-val",
            },
        )
    )

    debug_adapter.set_debug_context(context)
    try:
        result = await debug_adapter.send(_MESSAGES, model_id="gpt-5.2")
    finally:
        await debug_adapter.aclose()

    # The Adapter still receives the body httpx decoded from the wire.
    assert result == _COMPLETION

    [trace] = _traces(debug_store)
    wire_body = route.calls.last.request.content.decode("utf-8")
    assert (trace["type"], trace["provider_id"], trace["model_id"]) == (
        "provider_request",
        "debug",
        "gpt-5.2",
    )
    assert trace["context"] == {
        "run_id": context.run_id,
        "agent_id": context.agent_id,
        "session_id": context.session_id,
        "connection_id": context.connection_id,
        "iteration_number": context.iteration_number,
        "streaming": False,
    }
    request = trace["request"]
    assert (request["method"], request["url"], request["body"]) == ("POST", _DEBUG_URL, wire_body)
    assert request["headers"]["authorization"] == _REDACTED
    assert request["headers"]["x-custom-header"] == "test-value"
    response = trace["response"]
    assert response["status_code"] == 200
    # The body is recorded with its Content-Encoding undone; the header stays.
    assert json.loads(response["body"]) == _COMPLETION
    assert response["headers"].get("content-encoding") == content_encoding
    assert {name: response["headers"][name] for name in ("x-request-id", "x-debug-secret")} == {
        "x-request-id": "req-001",
        "x-debug-secret": _REDACTED,
    }
    assert response["headers"]["x-refresh-token"] == _REDACTED
    assert response["headers"]["x-api-key"] == _REDACTED
    assert isinstance(trace["duration_ms"], int) and trace["duration_ms"] >= 0
    # The debug context never reaches the Provider-bound payload.
    assert not {*vars(context), "context"} & json.loads(wire_body).keys()


@respx.mock
@pytest.mark.asyncio
async def test_stream_persists_the_verbatim_sse_body_as_one_response(
    debug_adapter: OpenAICompatibleAdapter, debug_store: DebugTraceStore
) -> None:
    respx.post(_DEBUG_URL).mock(return_value=_sse(_SSE_BODY, {"X-Debug-Secret": "do-not-leak"}))

    debug_adapter.set_debug_context(_debug_context(streaming=True))
    assert await _drain(debug_adapter)

    [trace] = _traces(debug_store)
    assert "stream" not in trace
    assert trace["context"]["streaming"] is True
    assert trace["request"]["method"] == "POST"
    assert (trace["response"]["status_code"], trace["response"]["body"]) == (200, _SSE_BODY)
    assert trace["response"]["headers"]["x-debug-secret"] == _REDACTED


@respx.mock
@pytest.mark.asyncio
async def test_stream_ending_without_done_persists_the_partial_body(
    debug_adapter: OpenAICompatibleAdapter, debug_store: DebugTraceStore
) -> None:
    respx.post(_DEBUG_URL).mock(return_value=_sse(_SSE_FRAMES))

    debug_adapter.set_debug_context(_debug_context(streaming=True))
    with pytest.raises(NetworkError):
        await _drain(debug_adapter)

    [trace] = _traces(debug_store)
    assert "stream" not in trace
    assert _SSE_FRAMES in trace["response"]["body"]


@respx.mock
@pytest.mark.asyncio
async def test_each_retried_error_stream_persists_its_own_trace_with_the_error_body(
    debug_adapter: OpenAICompatibleAdapter, debug_store: DebugTraceStore
) -> None:
    route = respx.post(_DEBUG_URL).mock(
        return_value=httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}})
    )

    debug_adapter.set_debug_context(_debug_context(streaming=True))
    with (
        pytest.raises(ProviderRateLimitError),
    ):
        await _drain(debug_adapter)

    traces = _traces(debug_store)
    assert route.call_count > 1
    assert len(traces) == route.call_count
    assert {trace["response"]["status_code"] for trace in traces} == {429}
    assert all("Rate limit exceeded" in trace["response"]["body"] for trace in traces)
    assert all("stream" not in trace for trace in traces)
