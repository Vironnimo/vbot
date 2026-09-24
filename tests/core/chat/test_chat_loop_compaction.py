"""Tests for chat loop compaction."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
)
from core.compaction import (
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    CompactionService,
)
from core.runs import (
    COMPACTION_ABORTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
)
from core.tools import (
    ToolRegistry,
    tool_success,
)
from tests.core.chat.chat_loop_compaction_test_support import (
    JsonObject,
    _RealCompactionAdapter,
    _RealCompactionStorage,
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
)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_user_compaction_waits_for_tool_result_and_continues_run(
    tmp_path: Path, failure: bool
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["wait_test"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call-one", "name": "wait_test", "arguments": {}}],
            },
            {"content": "finished", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()

    async def handler(_context: Any, _arguments: Any) -> Any:
        started.set()
        await release.wait()
        return tool_success({"done": True})

    tools.register(
        "wait_test",
        "Test sentinel",
        {"type": "object", "properties": {}, "additionalProperties": False},
        handler,
    )
    runtime = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
        storage=StubStorage(
            {"auto": False, "threshold": 0.99, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 1_000_000}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Earlier context"))
    session.append(ChatMessage.assistant(model=agent.model, content="Earlier answer"))
    service = StubCompactionService(
        should_auto=False,
        checkpoint=ChatMessage.compaction_checkpoint(
            summary="SUMMARY_SENTINEL", projection=[], compacted_token_count=8000
        ),
        compact_error=RuntimeError("test failure") if failure else None,
    )
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    run = await loop.start_run("coder", "Continue", session_id=session.id)
    await asyncio.wait_for(started.wait(), 10)
    assert run.request_compaction()
    assert run.request_compaction()
    assert run.compaction_state == "pending"
    assert service.compact_calls == []
    release.set()
    result = await asyncio.wait_for(run.wait(), 10)
    assert result.content == "finished"
    assert len(service.compact_calls) == 1
    assert service.compact_calls[0]["message_roles"][-2:] == ["assistant", "tool"]
    assert service.compact_calls[0]["minimum_reclaim_tokens"] == MIN_AUTO_COMPACTION_RECLAIM_TOKENS
    assert run.compaction_state == "idle"
    roles = persisted_roles(session.load())
    if failure:
        assert "compaction_checkpoint" not in roles
        assert any(event.type == COMPACTION_ABORTED_EVENT for event in run.events)
    else:
        assert roles.index("compaction_checkpoint") > roles.index("tool")
        assert any(event.type == COMPACTION_COMPLETED_EVENT for event in run.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["summary_tail", "continuation"])
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("separate_summary", [False, True])
async def test_compaction_routes_session_context_through_selected_adapter(
    tmp_path: Path, strategy: str, manual: bool, separate_summary: bool
) -> None:
    class ContextAdapter(_RealCompactionAdapter):
        def request_context_kwargs(self, **context: Any) -> JsonObject:
            return {"_test_context": {**context, "adapter": id(self)}}

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    active = ContextAdapter(
        [{"content": "finished", "tool_calls": None}], summaries=["ACTIVE SUMMARY"]
    )
    summary = ContextAdapter([], summaries=["SEPARATE SUMMARY"])
    storage = _RealCompactionStorage(
        {
            "enabled": True,
            "trigger": {"type": "input_tokens", "tokens": 10_000},
            "strategy": {
                "type": strategy,
                **(
                    {
                        "tail_tokens": 100,
                        "summary_model": "other/summary" if separate_summary else None,
                    }
                    if strategy == "summary_tail"
                    else {}
                ),
            },
        },
        data_dir=tmp_path,
    )
    runtime = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=active,
        provider_ids={"openai", "other"},
        adapters_by_connection={"other:api-key": summary},
        storage=storage,
        models=StubModels({("openai", "gpt-5.2"): 1_000_000, ("other", "summary"): 1_000_000}),
    )
    session = runtime.chat_sessions.create("coder", session_id="child-session")
    session.append(ChatMessage.user("old context " * 8_000))
    session.append(ChatMessage.assistant(model=agent.model, content="old response " * 8_000))
    session.append(ChatMessage.user("recent request"))
    session.append(ChatMessage.assistant(model=agent.model, content="recent answer"))
    affinity = runtime.chat_sessions.prompt_cache_affinity_id(session.address)
    loop = build_chat_loop(runtime, compaction_service=CompactionService()).child_loop(
        nesting_depth=1
    )

    if manual:
        run = await loop.start_compaction_run("coder", session.id)
        await run.wait()
        snapshot = session.read_chat_history_snapshot(limit=1)
        terminal = run.events[-1]
        assert terminal.payload["history_persisted"] is True
        assert terminal.payload["history_cursor"] == snapshot.after_cursor
        assert snapshot.page.record_run_ids == (run.id,)
    else:
        await loop.send("coder", "Continue", session_id=session.id)

    selected = summary if separate_summary and strategy == "summary_tail" else active
    assert len(selected.stream_requests) == 1
    assert selected.stream_requests[0]["kwargs"]["_test_context"] == {
        "agent_id": "coder",
        "session_id": session.id,
        "project_id": None,
        "prompt_cache_affinity_id": affinity,
        "adapter": id(selected),
    }
    assert (active if selected is summary else summary).stream_requests == []
    assert sum(message.role == "compaction_checkpoint" for message in session.load()) == 1
    rotated = runtime.chat_sessions.prompt_cache_affinity_id(session.address)
    assert rotated != affinity
    if not manual:
        assert active.requests[0]["kwargs"]["_test_context"]["prompt_cache_affinity_id"] == rotated


def test_context_window_uses_the_selected_provider_connection(tmp_path: Path) -> None:
    model_key = ("openai", "gpt-5.4")
    models = StubModels(
        {model_key: 272_000},
        connection_context_windows={model_key: {"api-key": 1_050_000, "subscription": 272_000}},
    )

    api_agent = StubAgent(id="api", model="openai/gpt-5.4", allowed_tools=["*"])
    api_data_dir = tmp_path / "api"
    api_data_dir.mkdir()
    api_runtime = StubRuntime(
        data_dir=api_data_dir,
        agent=api_agent,
        adapter=StubAdapter([]),
        models=models,
    )
    api_loop = build_chat_loop(api_runtime)
    assert api_loop._requests.resolve_context_window(api_agent) == 1_050_000
    subscription_target = SimpleNamespace(
        provider_id="openai",
        connection_id="openai:subscription",
        model_id="gpt-5.4",
    )
    assert (
        api_loop._requests.resolve_context_window(api_agent, cast(Any, subscription_target))
        == 272_000
    )

    subscription_agent = StubAgent(
        id="subscription",
        model="openai/gpt-5.4::subscription",
        allowed_tools=["*"],
    )
    subscription_data_dir = tmp_path / "subscription"
    subscription_data_dir.mkdir()
    subscription_runtime = StubRuntime(
        data_dir=subscription_data_dir,
        agent=subscription_agent,
        adapter=StubAdapter([]),
        adapters_by_connection={"openai:subscription": StubAdapter([])},
        models=models,
    )
    assert (
        build_chat_loop(subscription_runtime)._requests.resolve_context_window(subscription_agent)
        == 272_000
    )


@pytest.mark.asyncio
async def test_failed_auto_compaction_retries_at_the_next_boundary(tmp_path: Path) -> None:
    """A failed attempt must leave the next eligible boundary retrying."""

    compaction_counts_at_tool_boundary: list[int] = []
    tools = ToolRegistry()

    async def probe(context, arguments):
        compaction_counts_at_tool_boundary.append(len(service.compact_calls))
        return tool_success({"done": True})

    tools.register("probe", "Probe", {"type": "object"}, probe)
    agent = StubAgent(id="coder", model="openai/test", allowed_tools=["*"])
    adapter = StubAdapter(
        [{"tool_calls": [{"id": "one", "name": "probe", "arguments": {}}]}, {"content": "Done"}]
    )
    service = StubCompactionService(
        should_auto=True, compact_error=RuntimeError("summary unavailable")
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
        storage=StubStorage({"auto": True}),
        models=StubModels({("openai", "test"): 1_000_000}),
    )
    session = runtime.chat_sessions.create("coder", session_id="test")
    session.append(ChatMessage.user("Earlier context"))
    session.append(ChatMessage.assistant(model=agent.model, content="Earlier answer"))
    try:
        result = await build_chat_loop(runtime, compaction_service=cast(Any, service)).send(
            "coder", "Work", session_id="test"
        )
        assert result.content == "Done"
        assert compaction_counts_at_tool_boundary == [1]
        # Pre-request check, post-Tool-batch check and final-response check each
        # retried; a failed attempt never suppresses a later eligible boundary.
        assert len(service.compact_calls) == 3
        assert not any(message.role == "compaction_checkpoint" for message in session.load())
    finally:
        await runtime.chat_runs.aclose()


class _RecordingCompactionService(StubCompactionService):
    """Record the Session history each automatic Compaction received."""

    def __init__(self) -> None:
        super().__init__(should_auto=True)
        self.compacted_contents: list[list[Any]] = []

    async def compact(self, messages: list[ChatMessage], **_kwargs: Any) -> ChatMessage:
        self.compacted_contents.append([message.content for message in messages])
        return ChatMessage.compaction_checkpoint(
            summary=f"SUMMARY {len(self.compacted_contents)}",
            projection=[],
            compacted_token_count=10,
        )


def _auto_compacting_runtime(tmp_path: Path, adapter: StubAdapter, **kwargs: Any) -> Any:
    return StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"]),
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 1_000_000}),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_edit_run_compacts_only_its_edited_lineage(tmp_path: Path) -> None:
    runtime = _auto_compacting_runtime(tmp_path, StubAdapter([{"content": "new answer"}]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    original = ChatMessage.user("old request")
    session.append_many(
        [
            original,
            ChatMessage.assistant(model="openai/gpt-5.2", content="old answer"),
            ChatMessage.user("later request"),
        ]
    )
    service = _RecordingCompactionService()
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))

    run = await loop.edit_run(
        "coder", "edited request", session_id="session-one", message_id=original.id
    )
    await run.wait()

    # The pre-request boundary sees the edited lineage, never the replaced tail.
    assert service.compacted_contents[0] == ["edited request"]
    assert persisted_roles(session.load_active())[:3] == [
        "user",
        "compaction_checkpoint",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_automatic_compaction_boundaries_never_reload_complete_history(
    tmp_path: Path,
) -> None:
    tools = ToolRegistry()
    tools.register(
        "probe",
        "Return a fixed value.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"value": 1}),
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call-one", "name": "probe", "arguments": {}}],
            },
            {"content": "done", "tool_calls": None},
        ]
    )
    runtime = _auto_compacting_runtime(tmp_path, adapter, tools=tools)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Earlier context"))
    service = _RecordingCompactionService()
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    store = runtime.chat_sessions._store
    reads = store.messages_since
    complete_reads: list[None] = []

    def recording_messages_since(address: Any, cursor: Any) -> Any:
        if cursor is None:
            complete_reads.append(None)
        return reads(address, cursor)

    store.messages_since = recording_messages_since
    await (await loop.start_run("coder", "Go", session_id="session-one")).wait()

    # Every boundary compacted, yet only the Run-start snapshot read full history.
    assert len(service.compacted_contents) == 3
    assert complete_reads == [None]
    active = session.load_active()
    assert persisted_roles(active).count("compaction_checkpoint") == 3


@pytest.mark.asyncio
async def test_session_compaction_policy_override_governs_automatic_compaction(
    tmp_path: Path,
) -> None:
    runtime = _auto_compacting_runtime(tmp_path, StubAdapter([{"content": "done"}]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Earlier context"))
    runtime.chat_sessions.mutate_metadata(
        session.address,
        lambda metadata: metadata.__setitem__(
            "compaction_policy",
            {
                "enabled": False,
                "trigger": {"type": "context_ratio", "threshold": 0.8},
                "strategy": {"type": "summary_tail", "tail_tokens": 15_000},
            },
        ),
    )
    service = _RecordingCompactionService()
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))

    await (await loop.start_run("coder", "Go", session_id="session-one")).wait()

    assert service.should_auto_calls == []
    assert service.compacted_contents == []
