"""Tests for streaming recovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import streaming as streaming_module
from core.chat.streaming import (
    StreamingAccumulator,
    StreamingChunkTimeoutError,
    StreamingProgressTimeoutError,
    StreamRecoveryAction,
    decide_stream_recovery,
    is_local_provider_base_url,
    iter_with_chunk_timeout,
)
from core.providers.errors import (
    NetworkError,
    ProviderStreamingUnsupportedError,
    ProviderTimeoutError,
)
from core.utils.errors import ProviderError

pytestmark = pytest.mark.asyncio


async def test_finish_delta_records_reason_without_visible_event() -> None:
    accumulator = StreamingAccumulator()

    visible = accumulator.add_delta({"type": "finish", "reason": "stop"})

    fields = accumulator.finalize_assistant_fields()
    assert visible == []
    assert fields.finish_reason == "stop"


async def test_iter_with_chunk_timeout_resets_after_each_delta() -> None:
    async def source() -> AsyncIteratorForTest:
        yield {"type": "content_delta", "text": "first"}
        await asyncio.sleep(0.01)
        yield {"type": "content_delta", "text": "second"}

    chunks = [chunk async for chunk in iter_with_chunk_timeout(source(), timeout_seconds=0.05)]

    assert chunks == [
        {"type": "content_delta", "text": "first"},
        {"type": "content_delta", "text": "second"},
    ]


async def test_iter_with_chunk_timeout_counts_tool_call_fragments_as_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0

    async def source() -> AsyncIteratorForTest:
        nonlocal clock
        yield {
            "type": "tool_call_delta",
            "id": "call-write",
            "name_delta": "write",
            "arguments_delta": '{"path":"plan.md","content":"',
        }
        clock += 0.03
        yield {
            "type": "tool_call_delta",
            "id": "call-write",
            "name_delta": "",
            "arguments_delta": 'large plan"}',
        }
        clock += 0.03
        yield {"type": "heartbeat"}

    monkeypatch.setattr(
        streaming_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock),
    )

    chunks = [
        chunk
        async for chunk in iter_with_chunk_timeout(
            source(),
            timeout_seconds=1.0,
            progress_timeout_seconds=0.05,
        )
    ]

    assert [chunk["arguments_delta"] for chunk in chunks if chunk["type"] == "tool_call_delta"] == [
        '{"path":"plan.md","content":"',
        'large plan"}',
    ]


async def test_iter_with_chunk_timeout_fails_on_stalled_delta() -> None:
    closed = False

    async def source() -> AsyncIteratorForTest:
        nonlocal closed
        try:
            yield {"type": "content_delta", "text": "first"}
            await asyncio.sleep(1)
            yield {"type": "content_delta", "text": "late"}
        finally:
            closed = True

    iterator = iter_with_chunk_timeout(source(), timeout_seconds=0.01)

    assert await anext(iterator) == {"type": "content_delta", "text": "first"}
    with pytest.raises(StreamingChunkTimeoutError):
        await anext(iterator)
    assert closed is True


async def test_iter_with_chunk_timeout_heartbeats_keep_transport_alive_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False
    clock = 0.0

    async def source() -> AsyncIteratorForTest:
        nonlocal clock, closed
        try:
            while True:
                clock += 0.03
                yield {"type": "heartbeat"}
        finally:
            closed = True

    monkeypatch.setattr(
        streaming_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock),
    )
    iterator = iter_with_chunk_timeout(
        source(),
        timeout_seconds=1.0,
        progress_timeout_seconds=0.08,
    )

    assert await anext(iterator) == {"type": "heartbeat"}
    assert await anext(iterator) == {"type": "heartbeat"}
    with pytest.raises(StreamingProgressTimeoutError):
        while True:
            await anext(iterator)
    assert closed is True


async def test_iter_with_chunk_timeout_model_delta_resets_progress_window() -> None:
    async def source() -> AsyncIteratorForTest:
        yield {"type": "heartbeat"}
        await asyncio.sleep(0.02)
        yield {"type": "content_delta", "text": "still working"}
        await asyncio.sleep(0.02)
        yield {"type": "finish", "reason": "stop"}

    chunks = [
        chunk
        async for chunk in iter_with_chunk_timeout(
            source(),
            timeout_seconds=0.2,
            progress_timeout_seconds=0.1,
        )
    ]

    assert chunks == [
        {"type": "heartbeat"},
        {"type": "content_delta", "text": "still working"},
        {"type": "finish", "reason": "stop"},
    ]


async def test_decide_recovery_streaming_unsupported_before_visible_falls_back() -> None:
    action = decide_stream_recovery(
        ProviderStreamingUnsupportedError("no streaming"),
        can_restart=True,
        has_partial_content=False,
    )

    assert action is StreamRecoveryAction.FALLBACK


async def test_decide_recovery_accepts_logically_finished_stream() -> None:
    action = decide_stream_recovery(
        NetworkError("missing transport terminator"),
        can_restart=True,
        has_partial_content=False,
        finish_received=True,
    )

    assert action is StreamRecoveryAction.ACCEPT_COMPLETE


async def test_decide_recovery_restartable_transient_before_visible_restarts() -> None:
    for error in (
        NetworkError("dropped"),
        ProviderTimeoutError("slow"),
        StreamingChunkTimeoutError("stalled"),
        ProviderError("overloaded", retryable=True),
    ):
        action = decide_stream_recovery(
            error,
            can_restart=True,
            has_partial_content=False,
        )

        assert action is StreamRecoveryAction.RESTART, type(error).__name__


async def test_decide_recovery_restartable_before_visible_interrupts_when_budget_exhausted() -> (
    None
):
    action = decide_stream_recovery(
        NetworkError("dropped"),
        can_restart=False,
        has_partial_content=False,
    )

    assert action is StreamRecoveryAction.INTERRUPT


async def test_decide_recovery_retryable_provider_error_fails_when_restart_budget_exhausted() -> (
    None
):
    action = decide_stream_recovery(
        ProviderError("overloaded", retryable=True),
        can_restart=False,
        has_partial_content=False,
    )

    assert action is StreamRecoveryAction.FAIL


async def test_decide_recovery_non_restartable_before_visible_fails() -> None:
    action = decide_stream_recovery(
        ProviderError("fatal", retryable=False),
        can_restart=True,
        has_partial_content=False,
    )

    assert action is StreamRecoveryAction.FAIL


async def test_decide_recovery_visible_with_content_preserves_partial() -> None:
    for error in (
        NetworkError("dropped"),
        StreamingChunkTimeoutError("stalled"),
        ProviderError("overloaded", retryable=True),
        ProviderStreamingUnsupportedError("no streaming"),
    ):
        action = decide_stream_recovery(
            error,
            can_restart=True,
            has_partial_content=True,
        )

        assert action is StreamRecoveryAction.PRESERVE_PARTIAL, type(error).__name__


async def test_decide_recovery_reasoning_only_restarts_when_budget_remains() -> None:
    action = decide_stream_recovery(
        NetworkError("dropped"),
        can_restart=True,
        has_partial_content=False,
    )

    assert action is StreamRecoveryAction.RESTART


async def test_decide_recovery_tool_preview_restarts_when_no_answer_text_exists() -> None:
    action = decide_stream_recovery(
        NetworkError("dropped after Tool Call preview"),
        can_restart=True,
        has_partial_content=False,
    )

    assert action is StreamRecoveryAction.RESTART


async def test_local_provider_base_url_detects_loopback_and_local_names() -> None:
    for url in (
        "http://localhost:11434",
        "http://localhost:11434/v1",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
        "http://ollama.local:11434",
        "http://box.localhost/v1",
    ):
        assert is_local_provider_base_url(url) is True, url


async def test_local_provider_base_url_detects_private_and_link_local_ips() -> None:
    for url in (
        "http://10.0.0.5:1234",
        "http://172.16.4.2:1234",
        "http://192.168.1.50:11434",
        "http://169.254.10.10:1234",
    ):
        assert is_local_provider_base_url(url) is True, url


async def test_local_provider_base_url_rejects_public_hosts() -> None:
    for url in (
        "https://api.openai.com/v1",
        "https://api.anthropic.com",
        "http://8.8.8.8:443",
    ):
        assert is_local_provider_base_url(url) is False, url


async def test_local_provider_base_url_rejects_missing_or_unparseable() -> None:
    assert is_local_provider_base_url(None) is False
    assert is_local_provider_base_url("") is False
    assert is_local_provider_base_url("not a url") is False


async def test_iter_with_chunk_timeout_disabled_never_aborts_on_silence() -> None:
    async def source() -> AsyncIteratorForTest:
        yield {"type": "content_delta", "text": "first"}
        await asyncio.sleep(0.02)
        yield {"type": "content_delta", "text": "second"}

    chunks = [chunk async for chunk in iter_with_chunk_timeout(source(), timeout_seconds=None)]

    assert chunks == [
        {"type": "content_delta", "text": "first"},
        {"type": "content_delta", "text": "second"},
    ]


AsyncIteratorForTest = Any
