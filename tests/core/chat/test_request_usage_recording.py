"""Every Model attempt keeps its Usage independently of saved Assistant output."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.providers.errors import NetworkError, ProviderError
from core.runs import RunCancelledError
from core.usage import UsageRecorder
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)
from tests.core.chat.usage_recorder_support import RecordingUsageRecorder


@pytest.fixture(autouse=True)
def fast_recovery(monkeypatch):
    monkeypatch.setattr("core.chat.recovery.compute_retry_delay", lambda *a, **kw: (0, False))


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_empty_or_restarted_attempt_keeps_reported_usage_and_links_saved_step(
    tmp_path, streaming
):
    adapter = StubAdapter(
        [
            {
                "content": "",
                "terminal_outcome": "stop",
                "usage": {"input_tokens": 20, "output_tokens": 4},
            },
            {"content": "Recovered", "usage": {"input_tokens": 30}},
        ],
        stream_responses=[
            [
                {"type": "usage", "input_tokens": 20, "output_tokens": 4},
                NetworkError("retry this attempt"),
            ],
            [
                {"type": "content_delta", "text": "Recovered"},
                {"type": "usage", "input_tokens": 30},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    recorder = runtime.usage_recorder = RecordingUsageRecorder()

    result = await build_chat_loop(runtime, streaming=streaming).send(
        "coder", "Work", session_id="usage-test"
    )

    assert len(recorder.calls) == 2
    failed, completed = recorder.calls
    assert failed["status"] == "failed"
    assert failed["usage"] == {
        "input_tokens": 20,
        "output_tokens": 4,
        "usage_call_id": failed["id"],
    }
    assert completed["status"] == "completed"
    assert completed["usage"]["input_tokens"] == 30
    assert completed["usage"]["output_tokens_estimated"] is True
    assert completed["usage"]["cost"]["source"] == "unknown"
    assert result.usage["usage_call_id"] == completed["id"]
    history = runtime.chat_sessions.get(session_address("coder", "usage-test")).load()
    assistants = [message for message in history if message.role == "assistant"]
    assert len(assistants) == 1
    assert assistants[0].usage["usage_call_id"] == completed["id"]
    assert completed["model"] == "openai/test"
    assert completed["session_id"] == "usage-test"
    assert completed["run_id"] == next(iter(runtime.chat_runs._runs.values())).id


@pytest.mark.asyncio
async def test_cancel_before_visible_output_keeps_reported_counters(tmp_path):
    waiting = asyncio.Event()

    class UsageThenWaitAdapter(StubAdapter):
        async def stream(self, messages, **kwargs):
            yield {"type": "usage", "input_tokens": 42}
            waiting.set()
            await asyncio.Event().wait()

    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/test"),
        adapter=UsageThenWaitAdapter([]),
    )
    recorder = runtime.usage_recorder = RecordingUsageRecorder()
    task = asyncio.create_task(
        build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="cancel-test")
    )
    await asyncio.wait_for(waiting.wait(), 3)
    run = next(iter(runtime.chat_runs._runs.values()))
    run.request_cancel()
    with pytest.raises(RunCancelledError):
        await task

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["status"] == "cancelled"
    assert recorder.calls[0]["usage"]["input_tokens"] == 42
    assert "output_tokens" not in recorder.calls[0]["usage"]
    history = runtime.chat_sessions.get(session_address("coder", "cancel-test")).load()
    assert not any(message.role == "assistant" for message in history)


@pytest.mark.asyncio
async def test_fatal_request_without_counters_remains_unknown(tmp_path):
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/test"),
        adapter=StubAdapter([ProviderError("not available", retryable=False)]),
    )
    recorder = runtime.usage_recorder = RecordingUsageRecorder()
    with pytest.raises(ProviderError):
        await build_chat_loop(runtime).send("coder", "Work", session_id="failed-test")

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["status"] == "failed"
    assert recorder.calls[0]["usage"] == {"usage_call_id": recorder.calls[0]["id"]}


@pytest.mark.asyncio
async def test_cancel_during_usage_persistence_keeps_visible_answer(tmp_path):
    saving = asyncio.Event()
    release = asyncio.Event()

    class WaitingUsageRecorder(RecordingUsageRecorder):
        async def finish(self, call_id, usage=None, *, status="completed"):
            saving.set()
            await release.wait()
            return await super().finish(call_id, usage, status=status)

    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/test"),
        adapter=StubAdapter(
            [],
            stream_responses=[
                [
                    {"type": "content_delta", "text": "Visible answer"},
                    {"type": "usage", "input_tokens": 42, "output_tokens": 5},
                    {"type": "finish", "reason": "stop"},
                ]
            ],
        ),
    )
    recorder = runtime.usage_recorder = WaitingUsageRecorder()
    task = asyncio.create_task(
        build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="cancel-test")
    )
    await asyncio.wait_for(saving.wait(), 3)
    run = next(iter(runtime.chat_runs._runs.values()))
    run.request_cancel()
    release.set()
    with pytest.raises(RunCancelledError):
        await task

    history = runtime.chat_sessions.get(session_address("coder", "cancel-test")).load()
    saved = next(message for message in history if message.role == "assistant")
    assert saved.content == "Visible answer"
    assert saved.usage["usage_call_id"] == recorder.calls[0]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_chat_records_canonical_usage_before_session_removal(tmp_path, streaming):
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/test"),
        adapter=StubAdapter(
            [
                {
                    "content": "Answer",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 7,
                        "future_usage_field": {"measurement": "preserve-me"},
                    },
                }
            ],
            stream_responses=[
                [
                    {"type": "content_delta", "text": "Answer"},
                    {"type": "usage", "input_tokens": 100, "output_tokens": 7},
                    {"type": "finish", "reason": "stop"},
                ]
            ],
        ),
    )
    recorder = runtime.usage_recorder = UsageRecorder(tmp_path / "model-usage.db")
    try:
        result = await build_chat_loop(runtime, streaming=streaming).send(
            "coder", "Work", session_id="durable-test"
        )
        history = runtime.chat_sessions.get(session_address("coder", "durable-test")).load()
        saved = next(message for message in history if message.role == "assistant")
        assert saved.usage["context_usage"] == result.usage["context_usage"]
        assert saved.usage["context_usage"]["tokens"] > 0
        if not streaming:
            assert saved.usage["future_usage_field"] == {"measurement": "preserve-me"}
        runtime.chat_sessions.delete(session_address("coder", "durable-test"))
        _, records = recorder.read_since()
        assert len(records) == 1
        assert records[0].id == result.usage["usage_call_id"]
        assert records[0].usage["input_tokens"] == 100
        assert records[0].usage["output_tokens"] == 7
        assert "estimated" not in records[0].usage
        assert "context_usage" not in records[0].usage
        assert "future_usage_field" not in records[0].usage
    finally:
        recorder.close()
