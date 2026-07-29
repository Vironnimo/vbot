"""Chat-loop tests grouped by compaction."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ChatLoop,
    ChatMessage,
)
from core.chat.chat import _RequestState, _RunRequest
from core.chat.continuation import (
    ContinuationTracker,
    inject_continuation_reminder,
    recover_continuation,
    render_continuation_reminder,
)
from core.chat.messages import HISTORY_COMPACTION_GUIDANCE
from core.runs import (
    COMPACTION_COMPLETED_EVENT,
    Run,
)
from core.tools import (
    HISTORY_TOOL_NAME,
    register_history_tool,
)
from tests.core.chat.chat_loop_support import (
    ClosingStubAdapter,
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubModels,
    StubProject,
    StubProjects,
    StubRuntime,
    StubSkill,
    StubSkills,
    StubStorage,
    build_chat_loop,
    persisted_roles,
)

JsonObject = dict[str, Any]


async def _maybe_auto_compact(
    loop: ChatLoop,
    agent: Any,
    adapter: Any,
    model_id: str,
    session: Any,
    messages: list[JsonObject],
    usage: JsonObject | None,
    *,
    run: Run,
    continuation_tracker: ContinuationTracker | None = None,
    continuation_reminder: str | None = None,
) -> list[JsonObject]:
    """Build the same Run context used by production before probing Compaction."""
    del agent, model_id
    prior_continuation = recover_continuation(session) if continuation_reminder else None
    context = loop._create_run_execution_context(
        run,
        _RunRequest(content="test"),
        session=session,
        prior_continuation=prior_continuation,
        continuation_reminder=continuation_reminder,
        continuation_tracker=continuation_tracker,
    )
    assert context.primary_target.adapter is adapter
    context.request_state = _RequestState(messages, [], (), ())
    state = await loop._maybe_auto_compact_state(
        context,
        context.primary_target,
        usage,
    )
    return state.messages


def test_compaction_latest_checkpoint_helper_returns_last_checkpoint() -> None:
    from core.chat.chat import _latest_compaction_checkpoint

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

    latest = _latest_compaction_checkpoint(
        [first_user, first_checkpoint, second_user, second_checkpoint]
    )

    assert latest is second_checkpoint


def test_compaction_latest_checkpoint_helper_returns_none_when_absent() -> None:
    from core.chat.chat import _latest_compaction_checkpoint

    assert _latest_compaction_checkpoint([ChatMessage.user("only")]) is None


def test_effective_compaction_messages_use_checkpoint_projection() -> None:
    from core.chat.messages import _effective_compaction_messages

    first = ChatMessage.user("first")
    second = ChatMessage.assistant(model="openai/gpt-5.2", content="second")
    third = ChatMessage.user("third")
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="s", projection=[second, third], compacted_token_count=1
    )

    effective = _effective_compaction_messages([first, second, third, checkpoint])

    assert [message.role for message in effective] == ["note", "assistant", "user"]
    assert effective[1:] == [second, third]


def test_effective_compaction_messages_append_newer_messages() -> None:
    from core.chat.messages import _effective_compaction_messages

    older = ChatMessage.user("older")
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="s", projection=[older], compacted_token_count=1
    )
    newer = ChatMessage.user("newer")

    effective = _effective_compaction_messages([older, checkpoint, newer])

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

    request_messages = asyncio.run(build_chat_loop(runtime)._build_request_messages(agent, session))

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

    request_messages = asyncio.run(build_chat_loop(runtime)._build_request_messages(agent, session))
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


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_skips_when_auto_disabled(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="unused",
        projection=[ChatMessage.user("unused")],
        compacted_token_count=1,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": False,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    messages = await build_chat_loop(runtime)._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    loop = build_chat_loop(
        runtime,
        compaction_service=cast(Any, compaction_service),
    )
    result = await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    assert result == messages
    assert compaction_service.should_auto_calls == []
    assert compaction_service.compact_calls == []


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_skips_when_threshold_not_reached(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2::subscription", allowed_tools=["*"])
    adapter = StubAdapter([])
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="unused",
        projection=[ChatMessage.user("unused")],
        compacted_token_count=1,
    )
    compaction_service = StubCompactionService(should_auto=False, checkpoint=checkpoint)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.95,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    messages = await build_chat_loop(runtime)._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    loop = build_chat_loop(
        runtime,
        compaction_service=cast(Any, compaction_service),
    )
    result = await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 20},
        run=run,
    )

    assert result == messages
    assert compaction_service.should_auto_calls == [(20, 100, 0.95)]
    assert compaction_service.compact_calls == []


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_skips_without_new_compactable_context(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    compaction_service = StubCompactionService(
        should_auto=True,
        has_compactable_context=False,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Keep working in this same turn"))
    messages = await build_chat_loop(runtime)._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    result = await _maybe_auto_compact(
        build_chat_loop(
            runtime,
            compaction_service=cast(Any, compaction_service),
        ),
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    assert result == messages
    assert len(compaction_service.compactable_context_calls) == 1
    assert compaction_service.should_auto_calls == []
    assert compaction_service.compact_calls == []


@pytest.mark.asyncio
async def test_compaction_resolves_floor_for_null_window_model(tmp_path: Path) -> None:
    # A model with no context window (None) must still drive auto-compaction:
    # the read-side default chain resolves the global floor so should_auto_compact
    # is called with a usable positive window instead of silently disabling.
    from core.providers.providers import GLOBAL_CONTEXT_WINDOW_FLOOR

    agent = StubAgent(id="coder", model="openai/gpt-5.2::subscription", allowed_tools=["*"])
    adapter = StubAdapter([])
    compaction_service = StubCompactionService(should_auto=False)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): None}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    messages = await build_chat_loop(runtime)._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    loop = build_chat_loop(
        runtime,
        compaction_service=cast(Any, compaction_service),
    )
    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 20},
        run=run,
    )

    assert compaction_service.should_auto_calls == [(20, GLOBAL_CONTEXT_WINDOW_FLOOR, 0.8)]


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_appends_checkpoint_and_rebuilds_messages(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    with caplog.at_level("INFO", logger="vbot.chat"):
        rebuilt = await _maybe_auto_compact(
            loop,
            agent,
            adapter,
            "gpt-5.2",
            session,
            messages,
            usage={"input_tokens": 90},
            run=run,
        )

    log_messages = [record.getMessage() for record in caplog.records]
    triggered_line = next(
        message for message in log_messages if message.startswith("Auto-compaction triggered")
    )
    assert "input_tokens=90" in triggered_line
    assert "context_window=100" in triggered_line
    completed_line = next(
        message for message in log_messages if message.startswith("Auto-compaction completed")
    )
    assert "session=session-one" in completed_line
    assert "estimated_tokens_after=" in completed_line
    assert persisted_roles(session.load()) == [
        "user",
        "assistant",
        "compaction_checkpoint",
    ]
    assert len(compaction_service.compact_calls) == 1
    assert compaction_service.compact_calls[0]["summary_model_id"] == "gpt-5.2"
    assert compaction_service.compact_calls[0]["summary_adapter"] is adapter
    assert [message["role"] for message in rebuilt] == ["system", "user", "user", "assistant"]
    assert rebuilt[1]["content"] == (
        "<system-reminder>\nCompacted tail context.\n\n"
        f"{HISTORY_COMPACTION_GUIDANCE.format(ordinal=1)}\n</system-reminder>"
    )
    assert rebuilt[2]["content"] == "Tail user"
    assert rebuilt[3]["content"] == "Tail assistant"
    assert any(event.type == COMPACTION_COMPLETED_EVENT for event in run.events)
    compaction_event = next(
        event for event in run.events if event.type == COMPACTION_COMPLETED_EVENT
    )
    assert compaction_event.payload["checkpoint"] == 1
    assert compaction_event.payload["checkpoint_id"] == checkpoint.id
    assert compaction_event.payload["history_available"] is True


@pytest.mark.asyncio
async def test_final_assistant_compaction_activates_history_on_next_run(tmp_path: Path) -> None:
    class CompactOnce:
        def __init__(self) -> None:
            self.compacted = False

        def estimate_messages_tokens(self, _messages: list[JsonObject]) -> int:
            return 90

        def has_new_compactable_context(
            self,
            _messages: list[ChatMessage],
            _settings: Any,
        ) -> bool:
            return True

        def should_auto_compact(
            self,
            _input_tokens: int,
            _context_window: int,
            _threshold: float,
        ) -> bool:
            return not self.compacted

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
            {"content": "First answer", "tool_calls": None},
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
    await loop.send("coder", "Second", session_id="session-one")

    first_names = [tool["name"] for tool in adapter.requests[0]["kwargs"]["tools"]]
    second_names = [tool["name"] for tool in adapter.requests[1]["kwargs"]["tools"]]
    assert HISTORY_TOOL_NAME not in first_names
    assert second_names == [HISTORY_TOOL_NAME]


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
    prior = recover_continuation(session)
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
        await loop._build_request_messages(agent, session),
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


@pytest.mark.asyncio
async def test_compaction_reuses_pinned_skill_catalog(tmp_path: Path) -> None:
    # The compaction rebuild must reuse the session's pinned catalog snapshot, so the
    # rebuilt system prompt's catalog is byte-identical across the checkpoint even if
    # the live registry grew since the session was pinned.
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
    runtime.skills = StubSkills([StubSkill("one", "One.", Path("a"))])
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    # Pin the session's catalog (as the first build would), then grow the registry.
    loop._pinned_skill_catalog("coder", "session-one", agent, runtime.skills, None)
    runtime.skills = StubSkills(
        [StubSkill("one", "One.", Path("a")), StubSkill("two", "Two.", Path("b"))]
    )
    calls_before = runtime.system_prompts.render_skill_catalog_calls

    messages = await loop._build_request_messages(agent, session)
    await _maybe_auto_compact(
        loop, agent, adapter, "gpt-5.2", session, messages, usage={"input_tokens": 90}, run=run
    )

    # No fresh render during compaction: the pinned snapshot was reused.
    assert runtime.system_prompts.render_skill_catalog_calls == calls_before


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_falls_back_when_summary_model_malformed(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": "malformed-summary-model",
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    assert len(compaction_service.compact_calls) == 1
    assert compaction_service.compact_calls[0]["summary_model_id"] == "gpt-5.2"
    assert compaction_service.compact_calls[0]["summary_adapter"] is adapter


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_falls_back_when_summary_adapter_lookup_fails(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        raise_on_connection={"missing-provider:api-key": KeyError("missing-provider:api-key")},
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": "missing-provider/gpt-5.2::api-key",
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    assert runtime.adapter_provider_id == "missing-provider"
    assert runtime.adapter_connection_id == "missing-provider:api-key"
    assert len(compaction_service.compact_calls) == 1
    assert compaction_service.compact_calls[0]["summary_model_id"] == "gpt-5.2"
    assert compaction_service.compact_calls[0]["summary_adapter"] is adapter


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_logs_warning_when_compaction_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    compaction_service = StubCompactionService(
        should_auto=True,
        compact_error=RuntimeError("compaction broke"),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    session.append(ChatMessage.assistant(model=agent.model, content="Hello"))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    with caplog.at_level("WARNING"):
        result = await _maybe_auto_compact(
            loop,
            agent,
            adapter,
            "gpt-5.2",
            session,
            messages,
            usage={"input_tokens": 90},
            run=run,
        )

    assert result == messages
    assert persisted_roles(session.load()) == ["user", "assistant"]
    assert any(
        "Compaction failed; continuing without compaction" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_compact_session_reports_unavailable_without_compaction_service(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    runtime.chat_sessions.create("coder", session_id="session-one")

    reply = await build_chat_loop(runtime).compact_session("coder", "session-one")

    assert reply == "Compaction is not available."


@pytest.mark.asyncio
async def test_compact_session_refuses_while_run_is_active(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="unused",
        projection=[ChatMessage.user("unused")],
        compacted_token_count=1,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=StubAdapter([]),
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    release = asyncio.Event()

    async def blocked_executor(run: Run) -> str:
        await release.wait()
        return "done"

    active_run = await runtime.chat_runs.start(
        agent_id="coder", session_id="session-one", executor=blocked_executor, project_id=None
    )
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    reply = await loop.compact_session("coder", "session-one")
    release.set()
    await active_run.wait()

    assert reply == "Cannot compact while a run is active for this session."
    assert compaction_service.compact_calls == []


@pytest.mark.asyncio
async def test_compact_session_appends_checkpoint_and_closes_adapter(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = ClosingStubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    reply = await loop.compact_session("coder", "session-one")

    assert reply == "Context compacted."
    assert persisted_roles(session.load()) == ["user", "assistant", "compaction_checkpoint"]
    assert len(compaction_service.compact_calls) == 1
    assert compaction_service.compact_calls[0]["summary_model_id"] == "gpt-5.2"
    assert compaction_service.compact_calls[0]["summary_adapter"] is adapter
    assert compaction_service.compact_calls[0]["storage"] is runtime.storage
    assert compaction_service.compact_calls[0]["instruction"] is None
    assert adapter.closed is True
    persisted_checkpoint = session.load()[-1]
    assert persisted_checkpoint.projection is not None
    assert HISTORY_COMPACTION_GUIDANCE.format(ordinal=1) in str(
        persisted_checkpoint.projection[0]["content"]
    )


@pytest.mark.asyncio
async def test_compact_session_scopes_to_project_session_and_agent(tmp_path: Path) -> None:
    # A /compact issued in a project chat must compact the project session and
    # resolve the project agent — never silently fall back to the identity session.
    identity_agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    project_agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = ClosingStubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=identity_agent,
        adapter=adapter,
        project_agents={("proj", "coder"): project_agent},
        projects=StubProjects({"proj": StubProject("proj", str(tmp_path), [])}),
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
    )
    # Same session id in both scopes, distinct content: the identity session must
    # stay untouched, proving the project scope is the one that was loaded.
    identity_session = runtime.chat_sessions.create("coder", session_id="session-one")
    identity_session.append(ChatMessage.user("identity tail"))
    project_session = runtime.chat_sessions.create(
        "coder", session_id="session-one", project_id="proj"
    )
    project_tail = ChatMessage.user("project tail")
    project_session.append(project_tail)
    project_session.append(ChatMessage.assistant(model=project_agent.model, content="project a"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=project_session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    reply = await loop.compact_session("coder", "session-one", project_id="proj")

    assert reply == "Context compacted."
    # Resolved the project agent, never the identity fallback.
    assert ("proj", "coder") in runtime.agent_resolver.calls
    assert (None, "coder") not in runtime.agent_resolver.calls
    # The project session got the checkpoint; the identity session is untouched.
    assert persisted_roles(project_session.load()) == [
        "user",
        "assistant",
        "compaction_checkpoint",
    ]
    assert persisted_roles(identity_session.load()) == ["user"]


@pytest.mark.asyncio
async def test_compact_session_forwards_instruction_to_service(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = ClosingStubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    reply = await loop.compact_session("coder", "session-one", "keep the API design")

    assert reply == "Context compacted."
    assert compaction_service.compact_calls[0]["instruction"] == "keep the API design"


@pytest.mark.asyncio
async def test_compact_session_converts_compaction_failure_into_reply(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    compaction_service = StubCompactionService(
        should_auto=True,
        compact_error=RuntimeError("compaction broke"),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=StubAdapter([]),
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    reply = await loop.compact_session("coder", "session-one")

    assert reply == "Compaction failed: compaction broke"
    assert persisted_roles(session.load()) == ["user"]
    request_state = await loop._build_request_state(agent, session)
    assert HISTORY_TOOL_NAME not in [tool["name"] for tool in request_state.tools]
