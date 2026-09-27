"""Stream stall guards, recovery decisions and local-provider detection."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import streaming as streaming_module
from core.chat.streaming import (
    StreamingChunkTimeoutError,
    StreamingProgressTimeoutError,
    StreamRecoveryAction,
    decide_stream_recovery,
    is_local_provider_base_url,
    iter_with_chunk_timeout,
)
from core.providers.errors import (
    NetworkError,
    ProviderRateLimitError,
    ProviderStreamingUnsupportedError,
)
from core.utils.errors import ProviderError

JsonObject = dict[str, Any]

_CHUNKS: list[JsonObject] = [
    {"type": "heartbeat"},
    {"type": "content_delta", "text": "still working"},
    {"type": "finish", "reason": "stop"},
]


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [0.0]
    monkeypatch.setattr(streaming_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return clock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout_seconds", "progress_timeout_seconds"),
    [(1.0, 1.0), (None, None)],
    ids=["guarded", "guards-disabled"],
)
async def test_iter_with_chunk_timeout_passes_every_chunk_through(
    timeout_seconds: float | None, progress_timeout_seconds: float | None
) -> None:
    async def source() -> AsyncIterator[JsonObject]:
        for chunk in _CHUNKS:
            yield chunk

    chunks = [
        chunk
        async for chunk in iter_with_chunk_timeout(
            source(),
            timeout_seconds=timeout_seconds,
            progress_timeout_seconds=progress_timeout_seconds,
        )
    ]

    assert chunks == _CHUNKS


@pytest.mark.asyncio
async def test_iter_with_chunk_timeout_fails_on_stalled_delta_and_closes_the_source() -> None:
    closed = False

    async def source() -> AsyncIterator[JsonObject]:
        nonlocal closed
        try:
            yield {"type": "content_delta", "text": "first"}
            await asyncio.Event().wait()
        finally:
            closed = True

    iterator = iter_with_chunk_timeout(source(), timeout_seconds=0.01)

    assert await anext(iterator) == {"type": "content_delta", "text": "first"}
    with pytest.raises(StreamingChunkTimeoutError):
        await anext(iterator)
    assert closed is True


@pytest.mark.asyncio
async def test_model_deltas_including_tool_call_fragments_reset_the_progress_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _fake_clock(monkeypatch)
    chunks: list[JsonObject] = [
        {"type": "tool_call_delta", "id": "call-write", "arguments_delta": '{"content":"'},
        {"type": "tool_call_delta", "id": "call-write", "arguments_delta": 'large plan"}'},
        {"type": "content_delta", "text": "still working"},
        {"type": "heartbeat"},
    ]

    async def source() -> AsyncIterator[JsonObject]:
        for chunk in chunks:
            yield chunk
            clock[0] += 0.03

    received = [
        chunk
        async for chunk in iter_with_chunk_timeout(
            source(), timeout_seconds=1.0, progress_timeout_seconds=0.05
        )
    ]

    assert received == chunks


@pytest.mark.asyncio
async def test_heartbeats_keep_the_transport_alive_but_not_model_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _fake_clock(monkeypatch)
    closed = False

    async def source() -> AsyncIterator[JsonObject]:
        nonlocal closed
        try:
            while True:
                clock[0] += 0.03
                yield {"type": "heartbeat"}
        finally:
            closed = True

    iterator = iter_with_chunk_timeout(source(), timeout_seconds=1.0, progress_timeout_seconds=0.08)

    assert await anext(iterator) == {"type": "heartbeat"}
    assert await anext(iterator) == {"type": "heartbeat"}
    with pytest.raises(StreamingProgressTimeoutError, match="produced no Model delta"):
        while True:
            await anext(iterator)
    assert closed is True


@pytest.mark.asyncio
async def test_recovery_deadline_stops_fresh_deltas_and_closes_the_stream() -> None:
    closed = asyncio.Event()

    async def progressing() -> AsyncIterator[JsonObject]:
        try:
            while True:
                yield {"type": "content_delta", "text": "x"}
                await asyncio.sleep(0.005)
        finally:
            closed.set()

    deltas = []
    with pytest.raises(StreamingProgressTimeoutError, match="recovery time budget"):
        async for delta in iter_with_chunk_timeout(
            progressing(),
            timeout_seconds=None,
            progress_timeout_seconds=None,
            deadline=time.monotonic() + 0.1,
        ):
            deltas.append(delta)
    assert deltas
    assert closed.is_set()


_ACCEPT = StreamRecoveryAction.ACCEPT_COMPLETE
_RESTART = StreamRecoveryAction.RESTART
_FALLBACK = StreamRecoveryAction.FALLBACK
_PRESERVE = StreamRecoveryAction.PRESERVE_PARTIAL
_INTERRUPT = StreamRecoveryAction.INTERRUPT
_FAIL = StreamRecoveryAction.FAIL


@pytest.mark.parametrize(
    ("error", "state", "action"),
    [
        (NetworkError("no terminator"), {"finish_received": True}, _ACCEPT),
        (ProviderStreamingUnsupportedError("no streaming"), {}, _FALLBACK),
        (NetworkError("dropped"), {}, _RESTART),
        (StreamingChunkTimeoutError("stalled"), {}, _RESTART),
        (ProviderError("overloaded", retryable=True), {}, _RESTART),
        (NetworkError("dropped"), {"can_restart": False}, _INTERRUPT),
        (ProviderError("overloaded", retryable=True), {"can_restart": False}, _FAIL),
        (ProviderRateLimitError("quota"), {"has_fallback_chain": True}, _FAIL),
        (ProviderError("fatal", retryable=False), {}, _FAIL),
        (NetworkError("dropped"), {"has_partial_content": True}, _PRESERVE),
        (ProviderStreamingUnsupportedError("no"), {"has_partial_content": True}, _PRESERVE),
        (ProviderError("fatal", retryable=False), {"has_partial_content": True}, _FAIL),
    ],
    ids=[
        "finished-stream",
        "unsupported-before-text",
        "drop-before-text",
        "stall-before-text",
        "retryable-before-text",
        "drop-restarts-exhausted",
        "retryable-restarts-exhausted",
        "rate-limit-with-chain",
        "fatal-before-text",
        "drop-after-text",
        "unsupported-after-text",
        "fatal-after-text",
    ],
)
def test_decide_stream_recovery(
    error: Exception, state: dict[str, bool], action: StreamRecoveryAction
) -> None:
    arguments = {"can_restart": True, "has_partial_content": False, **state}

    assert decide_stream_recovery(error, **arguments) is action


@pytest.mark.parametrize(
    ("base_url", "local"),
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:8080", True),
        ("http://[::1]:8080", True),
        ("http://ollama.local:11434", True),
        ("http://box.localhost/v1", True),
        ("http://10.0.0.5:1234", True),
        ("http://192.168.1.50:11434", True),
        ("http://169.254.10.10:1234", True),
        ("https://api.openai.com/v1", False),
        ("http://8.8.8.8:443", False),
        (None, False),
        ("", False),
        ("not a url", False),
    ],
)
def test_local_provider_base_url_detects_loopback_local_names_and_private_ips(
    base_url: str | None, local: bool
) -> None:
    assert is_local_provider_base_url(base_url) is local
