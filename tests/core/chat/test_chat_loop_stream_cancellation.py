"""Tests for chat loop stream cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat.continuation import (
    recover_continuation,
)
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY
from core.runs import (
    RunCancelledError,
    RunStatus,
)
from tests.core.chat.chat_loop_stream_recovery_test_support import (
    JsonObject,
)
from tests.core.chat.chat_loop_support import (
    BlockingReasoningStreamingStubAdapter,
    BlockingStreamingStubAdapter,
    PolicyStubAdapter,
    SilentBlockingStreamingStubAdapter,
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    persisted_roles,
    session_address,
)


class CompletedStreamingStubAdapter(StubAdapter):
    """Finish a visible stream, then let the test arrange the persist-lock race."""

    def __init__(self) -> None:
        super().__init__([])
        self.finish_emitted = asyncio.Event()
        self.release_stream = asyncio.Event()

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        del messages, model_id, kwargs
        yield {"type": "content_delta", "text": "Complete answer"}
        yield {"type": "finish", "reason": "stop"}
        self.finish_emitted.set()
        await self.release_stream.wait()


@pytest.mark.asyncio
async def test_user_cancel_after_visible_stream_preserves_partial_and_stays_cancelled(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStreamingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Hi", session_id="session-one"
    )
    await adapter.stream_started.wait()
    run.request_cancel(reason="user")
    await asyncio.sleep(0)

    # A user cancel mid visible stream still ends as cancelled — never
    # reclassified as a transient error or a completed run — but the answer the
    # user already saw is preserved as an interrupted assistant turn.
    with pytest.raises(RunCancelledError):
        await run.wait()

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].content == "before"
    assert messages[1].interrupted is True
    assert not any(message.error_kind for message in messages if message.role == "error")


@pytest.mark.asyncio
async def test_user_cancel_while_complete_stream_waits_to_persist_preserves_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = CompletedStreamingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Hi", session_id="session-one"
    )
    await adapter.finish_emitted.wait()

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
    release_holder.set()
    await holder_task

    with pytest.raises(RunCancelledError):
        await run.wait()

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert run.status == RunStatus.CANCELLED
    assert [message.role for message in messages] == ["user", "assistant", "run_summary"]
    assert messages[1].content == "Complete answer"
    assert messages[1].interrupted is False


@pytest.mark.asyncio
async def test_user_cancel_replays_interrupted_reasoning_only_through_checkpoint(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingReasoningStreamingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Hi", session_id="session-one"
    )
    await adapter.stream_started.wait()
    run.request_cancel(reason="user")
    await asyncio.sleep(0)

    with pytest.raises(RunCancelledError):
        await run.wait()

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].content is None
    assert messages[1].reasoning == "Thinking hard."
    assert messages[1].reasoning_meta == {"signature": "interrupted-signed-state"}
    assert messages[1].interrupted is True

    reasoning_events = [event for event in run.events if event.type == "reasoning"]
    assistant_events = [event for event in run.events if event.type == "assistant_output"]
    assert reasoning_events[-1].payload["message"]["reasoning"] == "Thinking hard."
    assert "reasoning_meta" not in reasoning_events[-1].payload["message"]
    assert assistant_events[-1].payload["message"]["interrupted"] is True

    state = await recover_continuation(
        runtime.chat_sessions.get(session_address("coder", "session-one"))
    )
    assert state is not None
    assert state.reasoning == "Thinking hard."
    summaries = [message for message in messages if message.role == "run_summary"]
    assert summaries[-1].status == "cancelled"

    followup_adapter = PolicyStubAdapter(
        [{"content": "Recovered safely.", "tool_calls": None}],
        policy=REASONING_REPLAY_FULL_HISTORY,
    )
    runtime.adapter = followup_adapter
    await build_chat_loop(runtime).send("coder", "Continue safely", session_id="session-one")

    request_messages = followup_adapter.requests[0]["messages"]
    reminder = next(
        str(message["content"])
        for message in request_messages
        if "<continuation-checkpoint" in str(message.get("content") or "")
    )
    assert "Thinking hard." in reminder
    assert not [message for message in request_messages if message["role"] == "assistant"]
    persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert persisted[1].reasoning == "Thinking hard."
    assert persisted[1].reasoning_meta == {"signature": "interrupted-signed-state"}


@pytest.mark.asyncio
async def test_user_cancel_before_visible_output_does_not_persist_assistant(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = SilentBlockingStreamingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Hi", session_id="session-one"
    )
    await adapter.stream_started.wait()
    run.request_cancel(reason="user")
    await asyncio.sleep(0)

    with pytest.raises(RunCancelledError):
        await run.wait()

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(messages) == ["user"]
    assert not any(event.type == "assistant_output" for event in run.events)
