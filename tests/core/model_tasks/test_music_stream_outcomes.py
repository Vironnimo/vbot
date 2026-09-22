"""Music stream completion and response ownership regressions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from core.model_tasks.music_providers import ProviderMusicClient
from core.providers.errors import NetworkError, ProviderError, ProviderTimeoutError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


@pytest.fixture
def client() -> ProviderMusicClient:
    return ProviderMusicClient(
        provider=ProviderConfig(
            id="openrouter",
            name="OpenRouter",
            adapter="openrouter",
            base_url="https://openrouter.ai/api/v1",
            connections=[],
        ),
        connection=ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key="TEST_KEY"),
        ),
        credential="test-key",
        model_id="google/lyria-3-pro-preview",
    )


def _event(choice: dict) -> bytes:
    return f"data: {json.dumps({'choices': [choice]})}\n\n".encode()


class _Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, failure: BaseException | None = None) -> None:
        self.body = body
        self.failure = failure
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["length", "content_filter", "error", "tool_calls", "unknown", [], {}]
)
@respx.mock
async def test_explicit_unsuccessful_finish_rejects_partial_audio(
    client: ProviderMusicClient, reason: object
) -> None:
    stream = _Stream(
        _event({"delta": {"audio": {"data": "YWJj"}}})
        + _event({"finish_reason": reason})
        + b"data: [DONE]\n\n"
    )
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").respond(200, stream=stream)

    with pytest.raises(ProviderError) as exc:
        await client.generate("Music", options={})

    assert not exc.value.retryable
    assert stream.closed
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["network_error", "server_error"])
@respx.mock
async def test_native_failure_cannot_be_overridden_by_stop(
    client: ProviderMusicClient, reason: str
) -> None:
    stream = _Stream(
        _event({"delta": {"audio": {"data": "YWJj"}}})
        + _event({"finish_reason": "stop", "native_finish_reason": reason})
        + b"data: [DONE]\n\n"
    )
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").respond(200, stream=stream)

    with pytest.raises(ProviderError):
        await client.generate("Music", options={})

    assert stream.closed
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_repeated_stop_frames_preserve_audio_and_transcript(
    client: ProviderMusicClient,
) -> None:
    stream = _Stream(
        _event({"delta": {"audio": {"data": "YW", "transcript": "A "}}})
        + _event(
            {"delta": {"audio": {"data": "Jj", "transcript": "song"}}, "finish_reason": "stop"}
        )
        + _event({"delta": {}, "finish_reason": "stop"})
        + b"data: [DONE]\n\n"
    )
    respx.post("https://openrouter.ai/api/v1/chat/completions").respond(200, stream=stream)

    result = await client.generate("Music", options={})

    assert result.data == b"abc"
    assert result.transcript == "A song"
    assert stream.closed


@pytest.mark.asyncio
@respx.mock
async def test_stop_without_done_cannot_return_audio(client: ProviderMusicClient) -> None:
    stream = _Stream(_event({"delta": {"audio": {"data": "YWJj"}}, "finish_reason": "stop"}))
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").respond(200, stream=stream)

    with pytest.raises(NetworkError):
        await client.generate("Music", options={})

    assert stream.closed
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize("cancel", [False, True])
@respx.mock
async def test_failed_or_cancelled_body_read_closes_response(
    client: ProviderMusicClient, status: int, cancel: bool
) -> None:
    failure = asyncio.CancelledError() if cancel else httpx.ReadTimeout("interrupted read")
    stream = _Stream(b"", failure)
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").respond(
        status, stream=stream
    )

    with pytest.raises(asyncio.CancelledError if cancel else ProviderTimeoutError):
        await client.generate("Music", options={})

    assert stream.closed
    assert route.call_count == 1
