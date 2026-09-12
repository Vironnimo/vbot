"""Tests for chat loop compaction checkpoint."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
)
from core.chat._request_history import (
    _restore_in_run_tool_result_content,
)
from core.chat._run_state import (
    RequestBuildInputs,
)
from core.chat.continuation import (
    ContinuationTracker,
    inject_continuation_reminder,
    recover_continuation,
    render_continuation_reminder,
)
from core.compaction import (
    TOOL_RESULT_COMPACTED_FIELD,
)
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.runs import (
    COMPACTION_ABORTED_EVENT,
    COMPACTION_STARTED_EVENT,
    Run,
)
from core.tools import (
    HISTORY_TOOL_NAME,
    ToolRegistry,
    register_history_tool,
    tool_success,
)
from core.utils.tokens import estimate_request_input_tokens
from tests.core.chat.chat_loop_compaction_test_support import (
    JsonObject,
    _maybe_auto_compact,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubModels,
    StubRuntime,
    StubStorage,
    build_chat_loop,
    persisted_roles,
    session_address,
)

_ASYNC_COORDINATION_TIMEOUT_SECONDS = 10.0


class _BlockingOnceCompactionService:
    """Pause one successful Compaction so a concurrent Session append can race it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.attempted = False
        self.checks = 0

    def has_new_compactable_context(
        self,
        _messages: list[ChatMessage],
        _settings: Any,
        **_kwargs: Any,
    ) -> bool:
        return True

    def should_auto_compact(
        self,
        _input_tokens: int,
        _context_window: int,
        _threshold: float,
        **_kwargs: Any,
    ) -> bool:
        self.checks += 1
        return self.checks == 2 and not self.attempted

    async def compact(
        self,
        messages: list[ChatMessage],
        **_kwargs: Any,
    ) -> ChatMessage:
        self.attempted = True
        self.started.set()
        await self.release.wait()
        return ChatMessage.compaction_checkpoint(
            summary="Compacted snapshot.",
            projection=messages,
            compacted_token_count=20,
        )


def test_compaction_latest_checkpoint_helper_returns_last_checkpoint() -> None:
    from core.chat._message_history import (
        latest_compaction_checkpoint,
    )

    first_user = ChatMessage.user("first")
    second_user = ChatMessage.user("second")
    first_checkpoint = ChatMessage.compaction_checkpoint(
        summary="checkpoint one",
        projection=[first_user],
        compacted_token_count=10,
    )
    second_checkpoint = ChatMessage.compaction_checkpoint(
        summary="checkpoint two",
        projection=[second_user],
        compacted_token_count=20,
    )

    latest = latest_compaction_checkpoint(
        [first_user, first_checkpoint, second_user, second_checkpoint]
    )

    assert latest is second_checkpoint


def test_compaction_latest_checkpoint_helper_returns_none_when_absent() -> None:
    from core.chat._message_history import (
        latest_compaction_checkpoint,
    )

    assert latest_compaction_checkpoint([ChatMessage.user("only")]) is None


def test_effective_compaction_messages_use_checkpoint_projection() -> None:
    from core.chat._message_history import effective_compaction_messages

    first = ChatMessage.user("first")
    second = ChatMessage.assistant(model="openai/gpt-5.2", content="second")
    third = ChatMessage.user("third")
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="s", projection=[second, third], compacted_token_count=1
    )

    effective = effective_compaction_messages([first, second, third, checkpoint])

    assert [message.role for message in effective] == ["note", "assistant", "user"]
    assert effective[1:] == [second, third]


def test_effective_compaction_messages_append_newer_messages() -> None:
    from core.chat._message_history import effective_compaction_messages

    older = ChatMessage.user("older")
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="s", projection=[older], compacted_token_count=1
    )
    newer = ChatMessage.user("newer")

    effective = effective_compaction_messages([older, checkpoint, newer])

    assert [message.role for message in effective] == ["note", "user", "user"]
    assert effective[1:] == [older, newer]


def test_compaction_build_request_messages_without_checkpoint_keeps_existing_path(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    session.append(ChatMessage.assistant(model=agent.model, content="Hello"))

    request_messages = asyncio.run(
        build_chat_loop(runtime)._requests._build_request_messages(agent, session)
    )

    assert [message["role"] for message in request_messages] == ["system", "user", "assistant"]
    assert request_messages[1]["content"] == "Hi"
    assert request_messages[2]["content"] == "Hello"


def test_compaction_build_request_messages_with_checkpoint_uses_summary_and_tail_only(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")

    session.append(ChatMessage.user("Old question"))
    session.append(ChatMessage.assistant(model=agent.model, content="Old answer"))
    tail_user = ChatMessage.user("Tail question")
    tail_assistant = ChatMessage.assistant(model=agent.model, content="Tail answer")
    session.append(tail_user)
    session.append(tail_assistant)
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted historical context.",
            projection=[tail_user, tail_assistant],
            compacted_token_count=123,
        )
    )

    request_messages = asyncio.run(
        build_chat_loop(runtime)._requests._build_request_messages(agent, session)
    )
    request_text = "\n".join(message.get("content", "") or "" for message in request_messages)

    assert [message["role"] for message in request_messages] == [
        "system",
        "user",
        "user",
        "assistant",
    ]
    assert request_messages[1]["content"] == (
        "<system-reminder>\nCompacted historical context.\n</system-reminder>"
    )
    assert request_messages[2]["content"] == "Tail question"
    assert request_messages[3]["content"] == "Tail answer"
    assert "Old question" not in request_text
    assert all(message["role"] != "compaction_checkpoint" for message in request_messages)


def test_compaction_does_not_restore_rich_content_for_aged_tool_result() -> None:
    call_id = "call-image"
    aged_content = json.dumps(
        {
            TOOL_RESULT_COMPACTED_FIELD: True,
            "tool": "read",
            "original_chars": 50_000,
            "outcome": {"ok": True},
        }
    )
    rebuilt = [{"role": "tool", "tool_call_id": call_id, "content": aged_content}]
    live = [
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": '{"ok":true}',
            TOOL_RESULT_CONTENT_BLOCKS_FIELD: [{"type": "text", "text": "rich"}],
        }
    ]

    restored = _restore_in_run_tool_result_content(rebuilt, live)

    assert TOOL_RESULT_CONTENT_BLOCKS_FIELD not in restored[0]


@pytest.mark.asyncio
async def test_final_assistant_compaction_activates_history_on_next_run(tmp_path: Path) -> None:
    class CompactOnce:
        def __init__(self) -> None:
            self.compacted = False
            self.checks = 0

        def estimate_messages_tokens(self, _messages: list[JsonObject]) -> int:
            return 90

        def has_new_compactable_context(
            self,
            _messages: list[ChatMessage],
            _settings: Any,
            **_kwargs: Any,
        ) -> bool:
            return True

        def should_auto_compact(
            self,
            _input_tokens: int,
            _context_window: int,
            _threshold: float,
            **_kwargs: Any,
        ) -> bool:
            self.checks += 1
            return self.checks == 2 and not self.compacted

        async def compact(self, messages: list[ChatMessage], **_kwargs: Any) -> ChatMessage:
            self.compacted = True
            return ChatMessage.compaction_checkpoint(
                summary="Compacted finished turn.",
                projection=messages[-2:],
                compacted_token_count=20,
            )

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = StubAdapter(
        [
            {
                "content": "First answer",
                "reasoning": "Provider-owned final-turn reasoning",
                "reasoning_meta": {"encrypted_content": "opaque-final-turn"},
                "tool_calls": None,
            },
            {"content": "Second answer", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, CompactOnce()))

    await loop.send("coder", "First", session_id="session-one")
    session = runtime.chat_sessions.get(session_address("coder", "session-one"))
    checkpoint = next(
        message for message in session.load() if message.role == "compaction_checkpoint"
    )
    post_compaction_state = await loop._requests.build_request_state(
        agent, session, inputs=RequestBuildInputs()
    )
    expected_context_tokens_after, _ = estimate_request_input_tokens(
        post_compaction_state.messages,
        post_compaction_state.tools,
    )

    assert checkpoint.usage is not None
    assert checkpoint.usage["context_tokens_after"] == expected_context_tokens_after
    messages_only_tokens, _ = estimate_request_input_tokens(post_compaction_state.messages)
    assert expected_context_tokens_after > messages_only_tokens

    await loop.send("coder", "Second", session_id="session-one")

    first_names = [tool["name"] for tool in adapter.requests[0]["kwargs"]["tools"]]
    second_names = [tool["name"] for tool in adapter.requests[1]["kwargs"]["tools"]]
    assert HISTORY_TOOL_NAME not in first_names
    assert second_names == [HISTORY_TOOL_NAME]


@pytest.mark.asyncio
async def test_final_assistant_compaction_releases_session_lock_during_model_call(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = StubAdapter(
        [
            {
                "content": "Finished",
                "tool_calls": None,
                "usage": {"input_tokens": 90, "output_tokens": 5},
            }
        ]
    )
    compaction_service = _BlockingOnceCompactionService()
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    run = await loop.start_run("coder", "Finish", session_id="session-one")
    await asyncio.wait_for(
        compaction_service.started.wait(), timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS
    )

    async def append_background_note() -> None:
        async with runtime.chat_sessions.write_lock(session_address("coder", "session-one")):
            runtime.chat_sessions.get(session_address("coder", "session-one")).add_note(
                "Background completed"
            )

    note_task = asyncio.create_task(append_background_note())
    try:
        await asyncio.wait_for(note_task, timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)
    finally:
        compaction_service.release.set()

    result = await run.wait()
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()

    assert result.content == "Finished"
    assert persisted_roles(messages) == ["user", "assistant", "note"]
    assert messages[-2].content == "Background completed"
    compaction_events = [
        event
        for event in run.events
        if event.type in {COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT}
    ]
    assert [event.type for event in compaction_events] == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_ABORTED_EVENT,
    ]
    assert compaction_events[-1].payload == {"reason": "stale_context"}


@pytest.mark.asyncio
async def test_mid_tool_stale_compaction_rebuilds_request_with_concurrent_note(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call-weather", "name": "get_weather", "arguments": {}}],
                "usage": {"input_tokens": 1_800, "output_tokens": 50},
            },
            {
                "content": "Sunny",
                "tool_calls": None,
                "usage": {"input_tokens": 200, "output_tokens": 50},
            },
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"weather": "sunny"}),
    )
    compaction_service = _BlockingOnceCompactionService()
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 2_000}),
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    run = await loop.start_run("coder", "Weather?", session_id="session-one")
    await asyncio.wait_for(
        compaction_service.started.wait(), timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS
    )

    async def append_background_note() -> None:
        async with runtime.chat_sessions.write_lock(session_address("coder", "session-one")):
            runtime.chat_sessions.get(session_address("coder", "session-one")).add_note(
                "Background completed"
            )

    note_task = asyncio.create_task(append_background_note())
    try:
        await asyncio.wait_for(note_task, timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)
    finally:
        compaction_service.release.set()

    result = await run.wait()
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    second_request_text = "\n".join(
        str(message.get("content", "")) for message in adapter.requests[1]["messages"]
    )

    assert result.content == "Sunny"
    assert persisted_roles(messages) == ["user", "assistant", "tool", "note", "assistant"]
    assert "<system-reminder>\nBackground completed\n</system-reminder>" in second_request_text
    assert [
        event.type
        for event in run.events
        if event.type in {COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT}
    ] == [COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT]


@pytest.mark.asyncio
async def test_compaction_reinjects_the_active_continuation_checkpoint(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Original work"))
    session.append(ChatMessage.assistant(model=agent.model, content="Partial"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=session.load(),
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    interrupted_tracker = ContinuationTracker(
        session,
        run_id="run-one",
        request="Original work",
    )
    interrupted_tracker.record_stream_delta(reasoning="Keep this plan")
    await interrupted_tracker.interrupt("network")
    prior = await recover_continuation(session)
    assert prior is not None
    session.append(ChatMessage.user("Keep going"))
    active_tracker = ContinuationTracker(
        session,
        run_id="run-two",
        request="Keep going",
        prior_state=prior,
    )
    reminder = render_continuation_reminder(prior, context_window=100)
    messages = inject_continuation_reminder(
        await loop._requests._build_request_messages(agent, session),
        reminder,
    )
    run = Run(run_id="run-two", agent_id=agent.id, session_id=session.id)

    rebuilt = await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
        continuation_tracker=active_tracker,
        continuation_reminder=reminder,
    )

    reminder_messages = [
        message
        for message in rebuilt
        if "<continuation-checkpoint" in str(message.get("content") or "")
    ]
    assert len(reminder_messages) == 1
    assert "Keep this plan" in reminder_messages[0]["content"]
    await active_tracker.interrupt("network")
