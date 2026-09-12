"""Tests for chat loop compaction failures."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
)
from core.chat._run_state import (
    RequestBuildInputs,
    _RequestState,
)
from core.compaction import (
    CompactionService,
)
from core.prompts.pinned_context import (
    PINNED_SKILL_CATALOG_META_KEY,
    pinned_skill_catalog,
)
from core.runs import (
    COMPACTION_ABORTED_EVENT,
    COMPACTION_STARTED_EVENT,
    Run,
)
from core.tools import (
    HISTORY_TOOL_NAME,
    register_history_tool,
)
from tests.core.chat.chat_loop_compaction_test_support import (
    _maybe_auto_compact,
    _RealCompactionStorage,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubModels,
    StubRuntime,
    StubSkill,
    StubSkills,
    StubStorage,
    build_chat_loop,
    persisted_roles,
    session_address,
)


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
    messages = await loop._requests._build_request_messages(agent, session)
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
    messages = await loop._requests._build_request_messages(agent, session)
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
    messages = await loop._requests._build_request_messages(agent, session)
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
    assert [event.type for event in run.events] == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_ABORTED_EVENT,
    ]
    assert run.events[-1].payload == {"reason": "failed"}


@pytest.mark.asyncio
async def test_real_auto_compaction_truncation_preserves_history_skills_and_prompt_epoch(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"], allowed_skills=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Incomplete summary"},
                {"type": "finish", "reason": "output_truncated"},
            ]
        ],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=_RealCompactionStorage(
            {
                "enabled": True,
                "trigger": {"type": "input_tokens", "tokens": 1},
                "strategy": {"type": "summary_tail", "tail_tokens": 1, "summary_model": None},
            },
            data_dir=tmp_path,
        ),
        models=StubModels({("openai", "gpt-5.2"): 1_000_000}),
    )
    runtime.skills = StubSkills([StubSkill("one", "One.", Path("a"))])
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD CONTEXT " * 8_000))
    session.activate_skill_context("one", {"activation_content": "SKILL SENTINEL"})
    session.append(ChatMessage.assistant(model=agent.model, content="Old answer"))
    session.append(ChatMessage.user("Current request"))
    session.append(ChatMessage.assistant(model=agent.model, content="Current answer"))
    loop = build_chat_loop(runtime, compaction_service=CompactionService())
    pinned_skill_catalog(loop._dependencies, "coder", session.id, agent, runtime.skills, None)
    runtime.skills = StubSkills(
        [StubSkill("one", "One.", Path("a")), StubSkill("two", "Two.", Path("b"))]
    )
    messages = await loop._requests._build_request_messages(agent, session)
    original_history = session.load()
    original_skills = session.activated_skill_contents()
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    result = await _maybe_auto_compact(
        loop, agent, adapter, "gpt-5.2", session, messages, usage=None, run=run
    )

    request_after = await loop._requests.build_request_state(
        agent, session, inputs=RequestBuildInputs()
    )
    metadata = runtime.chat_sessions.get_metadata(session_address("coder", session.id))
    assert result == messages
    assert session.load() == original_history
    assert HISTORY_TOOL_NAME not in request_after.session_tool_grants
    assert session.activated_skill_contents() == original_skills
    assert metadata[PINNED_SKILL_CATALOG_META_KEY] == {"catalog_text": "catalog:1"}
    assert runtime.refresh_skills_for_calls == []
    assert len(adapter.stream_requests) == 1
    assert [event.type for event in run.events] == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_ABORTED_EVENT,
    ]


@pytest.mark.asyncio
async def test_compaction_projection_failure_does_not_persist_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
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
    session.append(ChatMessage.user("Hi"))
    session.append(ChatMessage.assistant(model=agent.model, content="Hello"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=session.load(),
        compacted_token_count=42,
    )
    loop = build_chat_loop(
        runtime,
        compaction_service=cast(
            Any,
            StubCompactionService(should_auto=True, checkpoint=checkpoint),
        ),
    )
    messages = await loop._requests._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    async def fail_projected_request(*_args: Any, **_kwargs: Any) -> _RequestState:
        raise RuntimeError("projected request broke")

    monkeypatch.setattr(loop._requests, "build_request_state", fail_projected_request)

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
    assert [event.type for event in run.events] == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_ABORTED_EVENT,
    ]
    assert any(
        "Post-compaction request projection failed" in record.message for record in caplog.records
    )
