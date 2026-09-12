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
