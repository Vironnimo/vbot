"""Prepared Chat request bytes preserve wire behavior without repeated encoding."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import httpx._content
import pytest
import respx

from core.providers import _http_shared
from core.providers.openai_compatible import OpenAICompatibleAdapter

from .openai_compatible_test_support import OPENAI_CONFIG, OPENAI_URL, SUCCESS_RESPONSE


class _RefreshingToken:
    def __init__(self) -> None:
        self.token = "before-refresh"
        self.refreshes = 0

    async def __call__(self) -> str:
        return self.token

    async def refresh_after_rejection(
        self, rejected_access_token: str, *, status_code: int, response_body: str
    ) -> str:
        assert rejected_access_token == self.token
        assert status_code == 401
        self.refreshes += 1
        self.token = "after-refresh"
        return self.token


_MESSAGES = [{"role": "user", "content": 'Grüße 😀 "quoted" \\ slash\n\tline\u2028end'}]


def _response(streaming: bool) -> httpx.Response:
    if streaming:
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"Hello!"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )
    return httpx.Response(200, json=SUCCESS_RESPONSE)


async def _invoke(adapter: OpenAICompatibleAdapter, streaming: bool) -> None:
    if streaming:
        chunks = [chunk async for chunk in adapter.stream(_MESSAGES, model_id="test-model")]
        assert any(chunk.get("text") == "Hello!" for chunk in chunks)
    else:
        assert await adapter.send(_MESSAGES, model_id="test-model") == SUCCESS_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("recovery", ["none", "transient", "oauth", "sampling"])
async def test_request_encodes_off_loop_once_per_payload_revision(
    monkeypatch: pytest.MonkeyPatch, streaming: bool, recovery: str
) -> None:
    getter = _RefreshingToken()
    async with OpenAICompatibleAdapter(OPENAI_CONFIG, getter) as adapter:
        payload = adapter._build_payload(_MESSAGES, "test-model")
        if streaming:
            adapter._prepare_stream_payload(payload)
        expected = httpx.Request("POST", OPENAI_URL, json=payload).content
        without_sampling = httpx.Request(
            "POST",
            OPENAI_URL,
            json={key: value for key, value in payload.items() if key != "temperature"},
        ).content
        # Checking this declared limit must use the bytes eventually sent.
        monkeypatch.setattr(adapter, "request_body_limit", lambda model_id: len(expected))
        replies = [_response(streaming)]
        if recovery == "transient":
            replies.insert(0, httpx.Response(503))
        elif recovery == "oauth":
            replies.insert(0, httpx.Response(401))
        elif recovery == "sampling":
            replies.insert(0, httpx.Response(400, text="Unsupported parameter: 'temperature'"))
        encoder = httpx._content.json_dumps
        encoding_threads: list[int] = []

        def record_encoding(*args: Any, **kwargs: Any) -> str:
            encoding_threads.append(threading.get_ident())
            return encoder(*args, **kwargs)

        monkeypatch.setattr(httpx._content, "json_dumps", record_encoding)
        with respx.mock as router, patch("core.utils.retry._sleep", new_callable=AsyncMock):
            route = router.post(OPENAI_URL).mock(side_effect=replies)
            await _invoke(adapter, streaming)

        assert len(encoding_threads) == (2 if recovery == "sampling" else 1)
        assert all(thread != threading.get_ident() for thread in encoding_threads)
        assert route.calls[0].request.content == expected
        assert route.call_count == (1 if recovery == "none" else 2)
        if route.call_count == 2:
            assert route.calls[1].request.content == (
                without_sampling if recovery == "sampling" else expected
            )
        for call in route.calls:
            assert call.request.headers.get_list("content-type") == ["application/json"]
            assert int(call.request.headers["content-length"]) == len(call.request.content)
        assert getter.refreshes == (1 if recovery == "oauth" else 0)
        assert route.calls[-1].request.headers["authorization"] == f"Bearer {getter.token}"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("header_source", ["request", "client"])
async def test_prepared_body_preserves_case_insensitive_content_type(
    monkeypatch: pytest.MonkeyPatch, streaming: bool, header_source: str
) -> None:
    async with OpenAICompatibleAdapter(OPENAI_CONFIG, "test-key") as adapter:
        if header_source == "client":
            adapter._client.headers["content-type"] = "application/test-client+json"
            content_type = "application/test-client+json"
        else:
            # A request header must also win over the client's default.
            adapter._client.headers["content-type"] = "application/test-client+json"
            content_type = "application/test-request+json"
            monkeypatch.setattr(
                adapter,
                "_build_request_headers",
                AsyncMock(return_value={"content-type": content_type, "X-Test": "preserved"}),
            )
        with respx.mock as router:
            route = router.post(OPENAI_URL).mock(return_value=_response(streaming))
            await _invoke(adapter, streaming)
        assert route.calls[0].request.headers.get_list("content-type") == [content_type]
        if header_source == "request":
            assert route.calls[0].request.headers["x-test"] == "preserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
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
    async with OpenAICompatibleAdapter(OPENAI_CONFIG, "test-key") as adapter:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(OPENAI_URL).mock(return_value=_response(streaming))
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
