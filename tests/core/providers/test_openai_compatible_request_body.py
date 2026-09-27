"""Prepared Chat request bytes: exact encoding, reuse across retries and the size limit."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import httpx._content
import pytest
import respx

from core.providers import _http_shared
from core.providers.errors import ProviderRequestTooLargeError
from core.providers.openai_compatible import OpenAICompatibleAdapter

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

_MESSAGES = [{"role": "user", "content": 'Grüße 😀 "quoted" \\ slash\n\tline end'}]
_MODES = pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])


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
    """Retries reuse the prepared bytes; only a sampling fallback re-encodes its new payload.

    Headers, including the credential, are rebuilt for every attempt.
    """
    tokens = _TokenSource()
    encoder = httpx._content.json_dumps
    encoding_threads: list[int] = []

    def record_encoding(*args: Any, **kwargs: Any) -> str:
        encoding_threads.append(threading.get_ident())
        return encoder(*args, **kwargs)

    replies = [first_reply(), _reply(streaming)]  # built before encodings are recorded
    async with make_adapter(token_getter=tokens) as adapter:
        with (
            monkeypatch.context() as patched,
            respx.mock as router,
            patch("core.utils.retry._sleep", new_callable=AsyncMock),
        ):
            patched.setattr(httpx._content, "json_dumps", record_encoding)
            route = router.post(OPENAI_URL).mock(side_effect=replies)
            await _invoke(adapter, streaming)

    sampling = recovery == "sampling"
    assert len(encoding_threads) == (2 if sampling else 1)
    assert threading.get_ident() not in encoding_threads
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
    async def _build_request_headers(
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
    encode = _http_shared._encode_json_body

    def blocked_encode(payload: dict[str, Any]) -> bytes:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return encode(payload)

    monkeypatch.setattr(_http_shared, "_encode_json_body", blocked_encode)
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
