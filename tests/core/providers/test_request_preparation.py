"""Prepared Provider requests: built and encoded off the Event Loop, exact bytes and limits.

Every request path builds its payload in one request-body worker hop that also
encodes and checks the body, in the caller's context; retries resend those
bytes, and an established stream releases them.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, override

import httpx
import httpx._content
import pytest
import respx

from core.providers import http
from core.providers.adapter import request_input_budget, resolve_request_input_budget
from core.providers.errors import ProviderRequestTooLargeError
from core.providers.http import PreparedRequest
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import RequestInput
from core.providers.wire_profile import RequestRules

from . import anthropic_test_support as anthropic
from . import ollama_test_support as ollama
from . import openai_test_support as openai
from . import opencode_go_test_support as go
from . import wire_paths_test_support as paths
from .openai_compatible_test_support import (
    MODEL_ID,
    OPENAI_CONFIG,
    OPENAI_URL,
    SUCCESS_RESPONSE,
    collect,
    make_adapter,
    sse,
    sse_response,
)

_MESSAGES = [{"role": "user", "content": 'Grüße 😀 "quoted" \\ slash\n\tline end'}]
_MODES = pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
_BUDGET = RequestInput.measured(4321, estimated_change=21)


def _messages_sse() -> httpx.Response:
    return anthropic.sse_response(
        anthropic.sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ok"},
            },
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        )
    )


_STREAMING_PATHS = [
    pytest.param(paths.CHAT.streaming(paths.chat_sse), id="chat-stream"),
    pytest.param(paths.MESSAGES.streaming(lambda: _messages_sse()), id="messages-stream"),
    pytest.param(paths.PLATFORM.streaming(paths.responses_sse), id="platform-responses-stream"),
    pytest.param(paths.CODEX.streaming(paths.responses_sse), id="codex-sse-stream"),
    pytest.param(paths.OPENROUTER.streaming(paths.responses_sse), id="openrouter-stream"),
    pytest.param(
        paths.COPILOT_RESPONSES.streaming(paths.responses_sse), id="copilot-responses-stream"
    ),
    pytest.param(paths.COPILOT_MESSAGES_STREAM, id="copilot-messages-stream"),
    pytest.param(
        paths.GO_RESPONSES.streaming(lambda: go.success_response("responses", streaming=True)),
        id="go-responses-stream",
    ),
    pytest.param(
        paths.GO_MESSAGES.streaming(lambda: go.success_response("messages", streaming=True)),
        id="go-inner-messages-stream",
    ),
    pytest.param(paths.ZEN_GEMINI_STREAM, id="zen-gemini-stream"),
    pytest.param(
        paths.OLLAMA.streaming(lambda: ollama.ndjson(ollama.TEXT_RESPONSE)),
        id="ollama-native-stream",
    ),
]

_EVERY_HTTP_PATH = [
    pytest.param(paths.CHAT, id="chat-send"),
    pytest.param(paths.MESSAGES, id="messages-send"),
    pytest.param(paths.PLATFORM, id="platform-responses-send"),
    pytest.param(paths.CODEX, id="codex-sse-send"),
    pytest.param(paths.OPENROUTER, id="openrouter-send"),
    pytest.param(paths.COPILOT_RESPONSES, id="copilot-responses-send"),
    pytest.param(paths.COPILOT_MESSAGES, id="copilot-messages-send"),
    pytest.param(paths.GO_RESPONSES, id="go-responses-send"),
    pytest.param(paths.GO_MESSAGES, id="go-inner-messages-send"),
    pytest.param(paths.ZEN_RESPONSES, id="zen-responses-send"),
    pytest.param(paths.ZEN_GEMINI, id="zen-gemini-send"),
    pytest.param(paths.OLLAMA, id="ollama-native-send"),
    *_STREAMING_PATHS,
]


def _reply(streaming: bool) -> httpx.Response:
    if streaming:
        return sse_response(
            sse({"choices": [{"delta": {"content": "Hello!"}, "finish_reason": "stop"}]})
        )
    return httpx.Response(200, json=SUCCESS_RESPONSE)


async def _invoke(adapter: OpenAICompatibleAdapter, streaming: bool) -> None:
    if streaming:
        assert {"type": "content_delta", "text": "Hello!"} in await collect(adapter, _MESSAGES)
    else:
        assert await adapter.send(_MESSAGES, model_id=MODEL_ID) == SUCCESS_RESPONSE


def _httpx_json_bytes(payload: dict[str, Any]) -> bytes:
    return httpx.Request("POST", OPENAI_URL, json=payload).content


class _Preparation:
    """Records the thread and visible Chat budget of every payload build and body encoding.

    Every payload builder applies its request body rules once, last, so that
    call marks where the build ran. Replies built with ``json=`` must exist
    before :meth:`record` starts, or their encoding is recorded too.
    """

    def __init__(self, model_id: str = MODEL_ID) -> None:
        self.model_id = model_id
        self.builds: list[tuple[int, RequestInput]] = []
        self.encodings: list[int] = []
        self.hops = 0

    def record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        apply_body = RequestRules.apply_body
        encode = httpx._content.json_dumps
        hop = http._REQUEST_BODY_WORKERS.run

        def record_build(rules: RequestRules, payload: dict[str, Any]) -> None:
            budget = resolve_request_input_budget(self.model_id, -1)
            self.builds.append((threading.get_ident(), budget))
            apply_body(rules, payload)

        def record_encoding(*args: Any, **kwargs: Any) -> str:
            self.encodings.append(threading.get_ident())
            return encode(*args, **kwargs)

        async def count_hop(*args: Any, **kwargs: Any) -> Any:
            self.hops += 1
            return await hop(*args, **kwargs)

        monkeypatch.setattr(RequestRules, "apply_body", record_build)
        monkeypatch.setattr(httpx._content, "json_dumps", record_encoding)
        monkeypatch.setattr(http._REQUEST_BODY_WORKERS, "run", count_hop)

    @property
    def build_threads(self) -> list[int]:
        return [thread for thread, _budget in self.builds]


@pytest.mark.parametrize("path", _EVERY_HTTP_PATH)
@pytest.mark.asyncio
async def test_every_request_path_builds_and_encodes_off_the_event_loop_in_one_hop(
    monkeypatch: pytest.MonkeyPatch, path: paths.RequestPath
) -> None:
    """The build sees the caller's Chat budget, so it resolves the same output limit."""

    adapter = path.adapter()
    reply = path.success()
    preparation = _Preparation(path.model_id)
    try:
        with respx.mock:
            respx.post(path.url).mock(return_value=reply)
            preparation.record(monkeypatch)
            with request_input_budget(path.model_id, _BUDGET):
                await paths.request(adapter, path, {})
    finally:
        await adapter.aclose()

    ((build_thread, budget),) = preparation.builds
    assert build_thread != threading.get_ident()
    assert budget == _BUDGET
    assert preparation.encodings == [build_thread]
    assert preparation.hops == 1


@pytest.mark.asyncio
async def test_codex_websocket_request_is_built_continued_and_encoded_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One hop builds the request; a second chains it to the socket's continuation."""

    socket = openai.FakeCodexWebSocket(
        [[{"type": "response.completed", "response": openai.COMPLETED_RESPONSE}]]
    )
    adapter = openai.codex_adapter(
        codex_websocket_connect=openai.FakeCodexWebSocketConnector([socket]),
        model_lookup=openai.bundled_model_lookup(),
    )
    preparation = _Preparation(paths.CODEX.model_id)
    preparation.record(monkeypatch)
    try:
        with request_input_budget(paths.CODEX.model_id, _BUDGET):
            stream = adapter.stream(
                paths.HELLO, model_id=paths.CODEX.model_id, conversation_id="a:s"
            )
            [_ async for _ in stream]
    finally:
        await adapter.aclose()

    ((build_thread, budget),) = preparation.builds
    assert build_thread != threading.get_ident()
    assert budget == _BUDGET
    assert preparation.hops == 2
    assert [payload["type"] for payload in socket.sent_payloads] == ["response.create"]


@pytest.mark.parametrize("path", _STREAMING_PATHS)
@pytest.mark.asyncio
async def test_an_established_stream_releases_its_prepared_request(
    monkeypatch: pytest.MonkeyPatch, path: paths.RequestPath
) -> None:
    """Nothing resends the request once a delta arrived, so its payload and body are dropped."""

    prepared: list[PreparedRequest] = []
    initialize = PreparedRequest.__init__

    def capture(request: PreparedRequest, *args: Any) -> None:
        initialize(request, *args)
        prepared.append(request)

    adapter = path.adapter()
    reply = path.success()
    monkeypatch.setattr(PreparedRequest, "__init__", capture)
    try:
        with respx.mock:
            respx.post(path.url).mock(return_value=reply)
            stream = adapter.stream(paths.HELLO, model_id=path.model_id)
            try:
                await anext(stream)
                (request,) = prepared
                with pytest.raises(RuntimeError):
                    _ = request.payload
                with pytest.raises(RuntimeError):
                    _ = request.body
            finally:
                await stream.aclose()
    finally:
        await adapter.aclose()


class _TokenSource:
    """Issues a fresh token per header build and accepts one rejected-token refresh."""

    def __init__(self) -> None:
        self.issued: list[str] = []
        self.refreshes = 0

    async def __call__(self) -> str:
        self.issued.append(f"token-{len(self.issued) + 1}")
        return self.issued[-1]

    async def refresh_after_rejection(
        self, rejected_access_token: str, *, status_code: int, response_body: str
    ) -> str:
        assert (rejected_access_token, status_code) == (self.issued[-1], 401)
        self.refreshes += 1
        return "refreshed"


@_MODES
@pytest.mark.parametrize(
    ("first_reply", "recovery"),
    [
        pytest.param(lambda: httpx.Response(503), "transient", id="transient-retry"),
        pytest.param(lambda: httpx.Response(401), "oauth", id="oauth-refresh"),
        pytest.param(
            lambda: httpx.Response(400, text="Unsupported parameter: 'temperature'"),
            "sampling",
            id="sampling-fallback",
        ),
    ],
)
@pytest.mark.asyncio
async def test_retries_resend_bytes_encoded_off_loop_once_per_payload_revision(
    monkeypatch: pytest.MonkeyPatch, streaming: bool, first_reply, recovery: str
) -> None:
    """Retries reuse the prepared bytes; only a sampling fallback rebuilds its new payload.

    The rebuild runs off the Event Loop in its own hop, like the first build.
    Headers, including the credential, are rebuilt for every attempt.
    """
    tokens = _TokenSource()
    preparation = _Preparation()
    replies = [first_reply(), _reply(streaming)]  # built before encodings are recorded
    async with make_adapter(token_getter=tokens) as adapter:
        with monkeypatch.context() as patched, respx.mock as router:
            preparation.record(patched)
            route = router.post(OPENAI_URL).mock(side_effect=replies)
            await _invoke(adapter, streaming)

    sampling = recovery == "sampling"
    assert len(preparation.encodings) == (2 if sampling else 1)
    assert threading.get_ident() not in preparation.encodings
    assert preparation.build_threads == preparation.encodings
    assert preparation.hops == len(preparation.encodings)
    first, retry = (call.request for call in route.calls)
    first_payload = json.loads(first.content)
    assert first.content == _httpx_json_bytes(first_payload)
    if sampling:
        assert "temperature" in first_payload
        first_payload.pop("temperature")
    assert retry.content == _httpx_json_bytes(first_payload)
    for request in (first, retry):
        assert request.headers.get_list("content-type") == ["application/json"]
        assert int(request.headers["content-length"]) == len(request.content)
    assert [request.headers["authorization"] for request in (first, retry)] == [
        f"Bearer {token}" for token in tokens.issued
    ]
    assert tokens.refreshes == (1 if recovery == "oauth" else 0)


@_MODES
@pytest.mark.asyncio
async def test_declared_body_limit_counts_the_exact_wire_bytes_before_io(
    monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    async with make_adapter() as adapter:
        with respx.mock as router:
            route = router.post(OPENAI_URL).mock(side_effect=lambda request: _reply(streaming))
            await _invoke(adapter, streaming)
            size = len(route.calls.last.request.content)

            monkeypatch.setattr(adapter, "request_body_limit", lambda model_id: size)
            await _invoke(adapter, streaming)
            monkeypatch.setattr(adapter, "request_body_limit", lambda model_id: size - 1)
            with pytest.raises(ProviderRequestTooLargeError) as exc_info:
                await _invoke(adapter, streaming)

    assert route.call_count == 2
    assert (exc_info.value.size_bytes, exc_info.value.max_bytes) == (size, size - 1)
    assert exc_info.value.retryable is False


class _CustomContentTypeAdapter(OpenAICompatibleAdapter):
    @override
    def _payload_request_headers(
        self, messages: list[dict[str, Any]], payload: Any
    ) -> dict[str, str]:
        return {"content-type": "application/test-request+json", "X-Test": "preserved"}


@_MODES
@pytest.mark.asyncio
async def test_prepared_body_keeps_a_request_content_type_case_insensitively(
    streaming: bool,
) -> None:
    async with _CustomContentTypeAdapter(OPENAI_CONFIG, "test-key") as adapter:
        with respx.mock as router:
            route = router.post(OPENAI_URL).mock(return_value=_reply(streaming))
            await _invoke(adapter, streaming)

    request = route.calls.last.request
    assert request.headers.get_list("content-type") == ["application/test-request+json"]
    assert request.headers["x-test"] == "preserved"


@_MODES
@pytest.mark.asyncio
async def test_cancelling_request_preparation_never_sends_late(
    monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    encode = http._encode_json_body

    def blocked_encode(payload: dict[str, Any]) -> bytes:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return encode(payload)

    monkeypatch.setattr(http, "_encode_json_body", blocked_encode)
    async with make_adapter() as adapter:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(OPENAI_URL).mock(return_value=_reply(streaming))
            pending = asyncio.create_task(_invoke(adapter, streaming))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                pending.cancel()
                # Let cancellation reach the preparation await while blocked.
                await asyncio.sleep(0)
                assert not pending.done()
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            assert route.call_count == 0
