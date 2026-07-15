"""Chat-loop tests grouped by continuation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatLoop,
    ChatMessage,
    ChatSessionError,
    ReplySurface,
)
from core.chat.content_blocks import ContentBlock, MediaBlock, TextBlock
from core.chat.continuation import (
    ContinuationTracker,
    recover_continuation,
)
from core.providers.errors import (
    NetworkError,
)
from core.providers.reasoning import (
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    ReasoningReplayPolicy,
)
from core.runs import (
    ActiveRunError,
    RunCancelledError,
    RunStatus,
)
from core.tools import (
    ToolRegistry,
    tool_success,
)
from tests.core.chat.chat_loop_support import (
    BlockingReasoningStreamingStubAdapter,
    BlockingStubAdapter,
    PolicyStubAdapter,
    StubAdapter,
    StubAgent,
    StubRuntime,
    TenToolsThenBlockingReasoningAdapter,
    persisted_roles,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_start_run_requires_existing_session(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(Exception, match="session does not exist"):
        await ChatLoop(runtime).start_run("coder", "Hi", session_id="missing-session")

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_continue_run_uses_checkpoint_without_appending_duplicate_user_message(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    interrupted_adapter = StubAdapter(
        [],
        stream_responses=[NetworkError("offline") for _ in range(3)],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=interrupted_adapter)
    loop = ChatLoop(runtime, streaming=True)

    with pytest.raises(NetworkError):
        await loop.send("coder", "Hi", session_id="session-one")

    adapter = StubAdapter([{"content": "Continued", "tool_calls": None}])
    runtime.adapter = adapter

    run = await ChatLoop(runtime).continue_run("coder", "session-one")
    assistant = await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert assistant.content == "Continued"
    assert persisted_roles(messages) == ["user", "error", "assistant"]
    assert sum(1 for message in messages if message.role == "user") == 1
    request_text = "\n".join(
        str(message.get("content") or "") for message in adapter.requests[0]["messages"]
    )
    assert "<continuation-checkpoint" in request_text
    assert "Original request(s):\nHi" in request_text
    assert not runtime.chat_sessions.get("coder", "session-one").continuation_path.exists()


@pytest.mark.asyncio
async def test_continue_run_appends_surface_before_synthetic_continue_instruction(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    interrupted_adapter = StubAdapter(
        [],
        stream_responses=[NetworkError("offline") for _ in range(3)],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=interrupted_adapter)
    with pytest.raises(NetworkError):
        await ChatLoop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")

    adapter = StubAdapter([{"content": "Continued", "tool_calls": None}])
    runtime.adapter = adapter
    run = await ChatLoop(runtime).continue_run(
        "coder",
        "session-one",
        reply_surface=ReplySurface.webui(),
    )
    await run.wait()

    session = runtime.chat_sessions.get("coder", "session-one")
    messages = session.load()
    surface_note_index = next(
        index
        for index, message in enumerate(messages)
        if message.role == "note" and str(message.content).startswith("[reply-surface] ")
    )
    assert all(message.role != "user" for message in messages[surface_note_index + 1 :])
    request_messages = adapter.requests[0]["messages"]
    surface_request_index = next(
        index
        for index, message in enumerate(request_messages)
        if "shown in the WebUI" in str(message.get("content"))
    )
    continuation_index = next(
        index
        for index, message in enumerate(request_messages)
        if "<continuation-checkpoint" in str(message.get("content"))
    )
    assert surface_request_index < continuation_index


@pytest.mark.asyncio
async def test_continue_run_raises_when_no_checkpoint_exists(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "unused", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    with pytest.raises(ChatSessionError, match="no interrupted work"):
        await ChatLoop(runtime).continue_run("coder", "session-one")

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_continue_run_rejects_second_run_for_same_session(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    interrupted_adapter = StubAdapter(
        [],
        stream_responses=[NetworkError("offline") for _ in range(3)],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=interrupted_adapter)
    with pytest.raises(NetworkError):
        await ChatLoop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")
    adapter = BlockingStubAdapter()
    runtime.adapter = adapter

    loop = ChatLoop(runtime)
    first_run = await loop.continue_run("coder", "session-one")
    await adapter.request_started.wait()

    with pytest.raises(ActiveRunError, match="active run"):
        await loop.continue_run("coder", "session-one")

    first_run.request_cancel(reason="user")
    adapter.release.set()
    with pytest.raises(RunCancelledError):
        await first_run.wait()


@pytest.mark.asyncio
async def test_discard_continuation_clears_checkpoint(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[NetworkError("offline") for _ in range(3)],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    loop = ChatLoop(runtime, streaming=True)
    with pytest.raises(NetworkError):
        await loop.send("coder", "Hi", session_id="session-one")

    loop.discard_continuation("coder", "session-one")

    assert loop.continuation_summary("coder", "session-one") is None


@pytest.mark.asyncio
async def test_content_block_request_is_serialized_in_continuation_journal(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[NetworkError("offline") for _ in range(3)],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    content: list[ContentBlock] = [
        TextBlock(type="text", text="Describe this image"),
        MediaBlock(
            type="media",
            attachment_id="attachment-one",
            filename="photo.jpg",
            media_type="image/jpeg",
        ),
    ]

    with pytest.raises(NetworkError):
        await ChatLoop(runtime, streaming=True).send(
            "coder",
            content,
            session_id="session-one",
        )

    state = recover_continuation(runtime.chat_sessions.get("coder", "session-one"))
    assert state is not None
    assert state.original_requests == [
        [
            {"type": "text", "text": "Describe this image"},
            {
                "type": "media",
                "attachment_id": "attachment-one",
                "filename": "photo.jpg",
                "media_type": "image/jpeg",
            },
        ]
    ]


@pytest.mark.asyncio
async def test_initial_session_validation_failure_creates_no_checkpoint(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "unused", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(ChatSessionError):
        await ChatLoop(runtime).start_run("coder", "work", session_id="missing-session")

    sessions_dir = runtime.chat_sessions.sessions_dir("coder")
    assert not list(sessions_dir.glob("*.continuation.jsonl"))


@pytest.mark.asyncio
async def test_internal_run_neither_consumes_nor_resolves_continuation(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Background work complete", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tracker = ContinuationTracker(session, run_id="interrupted-run", request="visible work")
    await tracker.interrupt("network")
    before = ChatLoop(runtime).continuation_summary("coder", "session-one")

    run = await ChatLoop(runtime).start_run(
        "coder",
        "background note",
        session_id="session-one",
        internal=True,
    )
    await run.wait()

    after = ChatLoop(runtime).continuation_summary("coder", "session-one")
    assert after == before
    assert all(
        "continuation-checkpoint" not in str(message.get("content") or "")
        for message in adapter.requests[0]["messages"]
    )


@pytest.mark.asyncio
async def test_cancel_then_immediate_queued_correction_receives_finalized_checkpoint(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    first_adapter = BlockingReasoningStreamingStubAdapter()
    second_adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Corrected"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=first_adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = ChatLoop(runtime, streaming=True)

    first_run = await loop.start_run("coder", "Use the first folder", session_id="session-one")
    await first_adapter.stream_started.wait()
    runtime.adapter = second_adapter
    first_run.request_cancel(reason="user")
    queued = await loop.queue_run(
        "coder",
        "Not this folder; use the second one",
        session_id="session-one",
    )

    with pytest.raises(RunCancelledError):
        await first_run.wait()
    second_run = await queued.future
    assistant = await second_run.wait()

    assert assistant.content == "Corrected"
    request_messages = second_adapter.stream_requests[0]["messages"]
    request_texts = [str(message.get("content") or "") for message in request_messages]
    correction_index = request_texts.index("Not this folder; use the second one")
    assert "<continuation-checkpoint" in request_texts[correction_index - 1]
    assert "Thinking hard." in request_texts[correction_index - 1]
    persisted = runtime.chat_sessions.get("coder", "session-one").load()
    assert [message.content for message in persisted if message.role == "user"] == [
        "Use the first folder",
        "Not this folder; use the second one",
    ]


@pytest.mark.asyncio
async def test_cancel_after_ten_tools_then_correction_reuses_canonical_results_once(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    first_adapter = TenToolsThenBlockingReasoningAdapter()
    second_adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Corrected from retained work"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    executions: list[str] = []
    tools = ToolRegistry()

    def get_weather(_context: Any, arguments: JsonObject) -> JsonObject:
        executions.append(str(arguments["city"]))
        return tool_success({"city": arguments["city"], "temperature": 22})

    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        get_weather,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=first_adapter,
        tools=tools,
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = ChatLoop(runtime, streaming=True)

    first_run = await loop.start_run("coder", "Inspect ten cities", session_id="session-one")
    await first_adapter.second_step_started.wait()
    runtime.adapter = second_adapter
    first_run.request_cancel(reason="user")
    queued = await loop.queue_run(
        "coder",
        "Use those results, but correct the conclusion",
        session_id="session-one",
    )

    with pytest.raises(RunCancelledError):
        await first_run.wait()
    second_run = await queued.future
    assistant = await second_run.wait()

    assert assistant.content == "Corrected from retained work"
    assert executions == ["Berlin"] * 10
    request_messages = second_adapter.stream_requests[0]["messages"]
    assert sum(message["role"] == "tool" for message in request_messages) == 10
    correction_index = next(
        index
        for index, message in enumerate(request_messages)
        if message.get("content") == "Use those results, but correct the conclusion"
    )
    reminder = str(request_messages[correction_index - 1]["content"])
    assert reminder.count("<continuation-checkpoint") == 1
    assert "Plan the batch. Inspect every result." in reminder
    assert "Review the completed batch. Prepare the final answer." in reminder
    assert reminder.count(": completed") == 10


@pytest.mark.asyncio
async def test_second_interrupted_continue_extends_same_checkpoint(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    first_adapter = StubAdapter(
        [],
        stream_responses=[NetworkError("offline") for _ in range(3)],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=first_adapter)
    loop = ChatLoop(runtime, streaming=True)
    with pytest.raises(NetworkError):
        await loop.send("coder", "Do the work", session_id="session-one")
    first_state = recover_continuation(runtime.chat_sessions.get("coder", "session-one"))
    assert first_state is not None

    runtime.adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Resume plan"},
                NetworkError("offline again"),
            ]
        ],
    )
    second_run = await loop.continue_run("coder", "session-one")
    with pytest.raises(NetworkError):
        await second_run.wait()

    second_state = recover_continuation(runtime.chat_sessions.get("coder", "session-one"))
    assert second_state is not None
    assert second_state.checkpoint_id == first_state.checkpoint_id
    assert second_state.origin_run_id == first_state.origin_run_id
    assert second_state.latest_run_id == second_run.id
    assert second_state.reasoning == "Resume plan"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [REASONING_REPLAY_CURRENT_RUN, REASONING_REPLAY_FULL_HISTORY],
)
async def test_continuation_reminder_is_single_and_provider_policy_neutral(
    tmp_path: Path,
    policy: ReasoningReplayPolicy,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = PolicyStubAdapter([{"content": "Done", "tool_calls": None}], policy=policy)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Original work"))
    tracker = ContinuationTracker(
        session,
        run_id="run-one",
        request="Original work",
    )
    tracker.record_stream_delta(reasoning="Readable plan")
    await tracker.interrupt("provider")

    run = await ChatLoop(runtime).continue_run("coder", "session-one")
    await run.wait()

    request_text = "\n".join(
        str(message.get("content") or "") for message in adapter.requests[0]["messages"]
    )
    assert request_text.count("<continuation-checkpoint") == 1
    assert "Readable plan" in request_text
    assert "reasoning_meta" not in request_text


@pytest.mark.asyncio
async def test_start_run_rejects_second_run_for_same_session(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    first_run = await ChatLoop(runtime).start_run("coder", "Hi", session_id="session-one")
    await adapter.request_started.wait()

    with pytest.raises(ActiveRunError, match="active run"):
        await ChatLoop(runtime).start_run("coder", "Again", session_id="session-one")

    first_run.request_cancel()
    adapter.release.set()
    with pytest.raises(RunCancelledError):
        await first_run.wait()


@pytest.mark.asyncio
async def test_start_run_allows_parallel_different_sessions(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    first_adapter = BlockingStubAdapter()
    second_adapter = StubAdapter([{"content": "Second", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=first_adapter)
    adapters = [first_adapter, second_adapter]
    runtime.get_adapter = lambda provider_id, connection_id: adapters.pop(0)  # type: ignore[method-assign]
    runtime.chat_sessions.create("coder", session_id="session-one")
    runtime.chat_sessions.create("coder", session_id="session-two")

    first_run = await ChatLoop(runtime).start_run("coder", "First", session_id="session-one")
    await first_adapter.request_started.wait()
    second_run = await ChatLoop(runtime).start_run("coder", "Second", session_id="session-two")

    second_assistant = await second_run.wait()
    first_run.request_cancel()
    first_adapter.release.set()

    assert second_assistant.content == "Second"
    with pytest.raises(RunCancelledError):
        await first_run.wait()


@pytest.mark.asyncio
async def test_cancelled_run_ignores_late_assistant_output(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await ChatLoop(runtime).start_run("coder", "Hi", session_id="session-one")
    await adapter.request_started.wait()
    run.request_cancel()
    adapter.release.set()

    with pytest.raises(RunCancelledError):
        await run.wait()

    session_messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(session_messages) == ["user"]
    assert "assistant_output" not in [event.type for event in run.events]
