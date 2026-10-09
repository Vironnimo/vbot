"""Cancelling a Run while its Model step is in flight: what stays durable and how the Run
ends."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, override

import pytest

from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    MODEL_STEP_USAGE_EVENT,
    RunCancelledError,
    RunStatus,
)
from tests.core.chat.chat_loop_streaming_test_support import JsonObject, stream_runtime
from tests.core.chat.chat_loop_support import (
    BlockingReasoningStreamingStubAdapter,
    BlockingStreamingStubAdapter,
    MidStreamCancelledStubAdapter,
    PolicyStubAdapter,
    SilentBlockingStreamingStubAdapter,
    StubAdapter,
    build_chat_loop,
    event_types,
    history,
    persisted_roles,
    session_address,
)


class CompletedStreamingStubAdapter(StubAdapter):
    """Finish a visible stream, then let the test choose its cancellation boundary."""

    def __init__(
        self, *, deltas: list[JsonObject] | None = None, finish_reason: str = "stop"
    ) -> None:
        super().__init__([])
        self.finish_emitted = asyncio.Event()
        self.release_stream = asyncio.Event()
        self.closed = False
        self.deltas = deltas or [{"type": "content_delta", "text": "Complete answer"}]
        self.finish_reason = finish_reason

    @override
    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        del messages, model_id, kwargs
        for delta in self.deltas:
            yield delta
        yield {"type": "finish", "reason": self.finish_reason}
        self.finish_emitted.set()
        await self.release_stream.wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_case", ["unfinished", "truncated", "tool_calls", "reasoning_phase"]
)
async def test_user_cancel_after_visible_stream_closes_the_adapter_and_keeps_the_partial(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    finish_case: str,
) -> None:
    caplog.set_level(logging.DEBUG, logger="vbot.runs")
    if finish_case == "unfinished":
        adapter: Any = BlockingStreamingStubAdapter()
        cancellation_boundary = adapter.stream_started
    else:
        deltas: list[JsonObject] = [{"type": "content_delta", "text": "before"}]
        if finish_case == "tool_calls":
            deltas.append(
                {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "call-unexecuted",
                    "name": "write",
                    "arguments_delta": '{"path":"unused"}',
                }
            )
        elif finish_case == "reasoning_phase":
            deltas.append({"type": "reasoning_delta", "text": "Unfinished thinking"})
        adapter = CompletedStreamingStubAdapter(
            deltas=deltas,
            finish_reason="output_truncated" if finish_case == "truncated" else "stop",
        )
        cancellation_boundary = adapter.finish_emitted
    runtime = stream_runtime(tmp_path, adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime).start_run("coder", "Hi", session_id="session-one")
    await cancellation_boundary.wait()
    run.request_cancel(reason="user")
    await asyncio.sleep(0)

    # A user cancel mid visible stream still ends as cancelled, never reclassified
    # as a transient error or a completed Run.
    with pytest.raises(RunCancelledError):
        await run.wait()

    messages = history(runtime)
    assert adapter.closed is True
    assert run.status == RunStatus.CANCELLED
    [terminal_log] = [
        record
        for record in caplog.records
        if record.name == "vbot.runs" and f"run={run.id}" in record.getMessage()
    ]
    assert terminal_log.levelno == logging.INFO
    assert "reason=user" in terminal_log.getMessage()
    # The already-shown partial answer is preserved as an interrupted turn
    # (GLOSSARY -> Cancel); the never-released late delta stays suppressed.
    assert persisted_roles(messages) == ["user", "assistant"]
    assert (messages[1].content, messages[1].interrupted) == ("before", True)
    assert messages[1].interruption_cause == "user"
    assert messages[1].tool_calls is None
    assert run.tool_call_count == 0
    assert messages[-1].role == "run_summary"
    assert (messages[-1].status, messages[-1].completion_reason) == ("cancelled", "user")
    expected_events = [
        "run_started",
        "user_message_persisted",
        ASSISTANT_OUTPUT_DELTA_EVENT,
        *(["reasoning"] if finish_case == "reasoning_phase" else []),
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "run_cancelled",
    ]
    assert [
        event
        for event in await event_types(runtime, run)
        if event not in {"tool_call_delta", "reasoning_delta"}
    ] == expected_events


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_boundary", ["stream_close", "persistence"])
async def test_user_cancel_after_complete_stream_preserves_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_boundary: str,
) -> None:
    adapter = CompletedStreamingStubAdapter()
    runtime = stream_runtime(tmp_path, adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime).start_run("coder", "Hi", session_id="session-one")
    await adapter.finish_emitted.wait()

    if cancel_boundary == "persistence":
        target_lock = runtime.chat_sessions.write_lock(session_address("coder", "session-one"))
        holder_acquired = asyncio.Event()
        release_holder = asyncio.Event()
        persist_wait_started = asyncio.Event()

        async def hold_write_lock() -> None:
            async with target_lock:
                holder_acquired.set()
                await release_holder.wait()

        class SignallingWriteLock:
            async def __aenter__(self) -> Any:
                persist_wait_started.set()
                return await target_lock.__aenter__()

            async def __aexit__(self, *exc_info: object) -> None:
                await target_lock.__aexit__(*exc_info)

        holder_task = asyncio.create_task(hold_write_lock())
        await holder_acquired.wait()
        monkeypatch.setattr(
            runtime.chat_sessions,
            "write_lock",
            lambda *_args, **_kwargs: SignallingWriteLock(),
        )
        adapter.release_stream.set()
        await persist_wait_started.wait()

    run.request_cancel(reason="user")
    await asyncio.sleep(0)
    if cancel_boundary == "persistence":
        release_holder.set()
        await holder_task
        monkeypatch.undo()

    with pytest.raises(RunCancelledError):
        await run.wait()

    session = runtime.chat_sessions.get(session_address("coder", "session-one"))
    messages = session.load()
    assert run.status == RunStatus.CANCELLED
    assert [message.role for message in messages] == ["user", "assistant", "run_summary"]
    assert messages[1].content == "Complete answer"
    assert messages[1].interrupted is False
    assert adapter.closed is True
    assert run.iteration_count == 1
    # A complete answer leaves nothing unfinished even though Stop won.
    followup_adapter = StubAdapter([{"content": "New answer", "tool_calls": None}])
    runtime.adapter = followup_adapter
    await build_chat_loop(runtime).send("coder", "New independent task", session_id="session-one")
    assert not any(
        "previous turn" in str(message.get("content") or "")
        for message in followup_adapter.requests[0]["messages"]
    )


@pytest.mark.asyncio
async def test_user_cancel_keeps_interrupted_reasoning_out_of_later_requests(
    tmp_path: Path,
) -> None:
    adapter = BlockingReasoningStreamingStubAdapter()
    runtime = stream_runtime(tmp_path, adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime).start_run("coder", "Hi", session_id="session-one")
    await adapter.stream_started.wait()
    run.request_cancel(reason="user")
    await asyncio.sleep(0)

    with pytest.raises(RunCancelledError):
        await run.wait()
    events = await runtime.timelines.events(run)

    messages = history(runtime)
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].content is None
    assert messages[1].reasoning == "Thinking hard."
    assert messages[1].reasoning_meta == {"signature": "interrupted-signed-state"}
    assert messages[1].interrupted is True

    reasoning_events = [event for event in events if event.type == "reasoning"]
    assistant_events = [event for event in events if event.type == "assistant_output"]
    assert reasoning_events[-1].payload["message"]["reasoning"] == "Thinking hard."
    assert "reasoning_meta" not in reasoning_events[-1].payload["message"]
    assert assistant_events[-1].payload["message"]["interrupted"] is True

    summaries = [message for message in messages if message.role == "run_summary"]
    assert summaries[-1].status == "cancelled"

    followup_adapter = PolicyStubAdapter(
        [{"content": "Recovered safely.", "tool_calls": None}],
        policy=REASONING_REPLAY_FULL_HISTORY,
    )
    runtime.adapter = followup_adapter
    await build_chat_loop(runtime).send("coder", "Continue safely", session_id="session-one")

    request_messages = followup_adapter.requests[0]["messages"]
    request_text = "\n".join(str(message.get("content") or "") for message in request_messages)
    # The reasoning-only turn is not sent; the next request only says why it stopped.
    assert "Thinking hard." not in request_text
    assert request_text.count("The user stopped your previous turn before it was complete.") == 1
    assert not [message for message in request_messages if message["role"] == "assistant"]
    persisted = history(runtime)
    assert persisted[1].reasoning == "Thinking hard."
    assert persisted[1].reasoning_meta == {"signature": "interrupted-signed-state"}


@pytest.mark.asyncio
async def test_user_cancel_before_visible_output_does_not_persist_assistant(
    tmp_path: Path,
) -> None:
    adapter = SilentBlockingStreamingStubAdapter()
    runtime = stream_runtime(tmp_path, adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime).start_run("coder", "Hi", session_id="session-one")
    await adapter.stream_started.wait()
    run.request_cancel(reason="user")
    # Output the Provider delivers after the cancel is discarded.
    adapter.release.set()
    await asyncio.sleep(0)

    with pytest.raises(RunCancelledError):
        await run.wait()
    events = await runtime.timelines.events(run)

    messages = history(runtime)
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(messages) == ["user"]
    assert not any(event.type == "assistant_output" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker",
    ["record_delivered", "_prepare_assistant_context"],
    ids=["first-preparation-step", "last-preparation-step"],
)
async def test_cancel_during_completed_answer_preparation_preserves_visible_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker: str
) -> None:
    from core.chat._agentic_progression import _CHAT_TRANSFORM_WORKERS

    adapter = CompletedStreamingStubAdapter()
    runtime = stream_runtime(tmp_path, adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    run = await build_chat_loop(runtime).start_run("coder", "Hi", session_id="session-one")
    await adapter.finish_emitted.wait()
    entered, release = asyncio.Event(), asyncio.Event()
    original = _CHAT_TRANSFORM_WORKERS.run

    async def pause_worker(function, *args, **kwargs):
        if function.__name__ == worker:
            entered.set()
            await release.wait()
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(_CHAT_TRANSFORM_WORKERS, "run", pause_worker)
    adapter.release_stream.set()
    await asyncio.wait_for(entered.wait(), 5)
    assert any(event.type == "assistant_output" for event in run.events)
    run.request_cancel(reason="user")
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()
    messages = history(runtime)
    assert [message.content for message in messages if message.role == "assistant"] == [
        "Complete answer"
    ]


@pytest.mark.asyncio
async def test_internal_cancellation_after_reasoning_leaves_only_its_summary(
    tmp_path: Path,
) -> None:
    runtime = stream_runtime(tmp_path, MidStreamCancelledStubAdapter([]))

    with pytest.raises(RunCancelledError):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    messages = history(runtime)
    assert persisted_roles(messages) == ["user"]
    assert (messages[-1].role, messages[-1].status) == ("run_summary", "cancelled")
    with sqlite3.connect(runtime.chat_sessions._store.path) as connection:
        assert connection.execute("SELECT count(*) FROM run_stream_drafts").fetchone() == (0,)
