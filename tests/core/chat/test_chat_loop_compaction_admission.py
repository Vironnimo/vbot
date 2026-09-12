"""Tests for chat loop compaction admission."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
    ToolCall,
)
from core.runs import (
    COMPACTION_STARTED_EVENT,
    Run,
)
from core.tools import (
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
)


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
    messages = await build_chat_loop(runtime)._requests._build_request_messages(agent, session)
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
    messages = await build_chat_loop(runtime)._requests._build_request_messages(agent, session)
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
async def test_compaction_keeps_measured_anchor_despite_higher_wire_estimate(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])

    class HighEstimateAdapter(StubAdapter):
        def estimate_request_input_tokens(
            self,
            _messages: list[JsonObject],
            *,
            model_id: str,
            tools: list[JsonObject] | None = None,
        ) -> int:
            del model_id, tools
            return 95

    adapter = HighEstimateAdapter([])
    compaction_service = StubCompactionService(
        should_auto=False,
        estimated_tokens=95,
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
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    messages = await build_chat_loop(runtime)._requests._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _maybe_auto_compact(
        build_chat_loop(runtime, compaction_service=cast(Any, compaction_service)),
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 20},
        run=run,
    )

    assert compaction_service.should_auto_calls == [(20, 100, 0.8)]
    assert compaction_service.estimate_calls == []


@pytest.mark.asyncio
async def test_compaction_new_run_estimates_selected_wire_instead_of_reusing_old_measurement(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])

    class WireEstimateAdapter(StubAdapter):
        def estimate_request_input_tokens(
            self,
            _messages: list[JsonObject],
            *,
            model_id: str,
            tools: list[JsonObject] | None = None,
        ) -> int:
            del model_id, tools
            return 95

    adapter = WireEstimateAdapter([])
    compaction_service = StubCompactionService(should_auto=False)
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
    session.append(ChatMessage.user("Earlier"))
    session.append(
        ChatMessage.assistant(
            model=agent.model,
            content="x" * 50_000,
            usage={"input_tokens": 20, "output_tokens": 0},
        )
    )
    session.append(ChatMessage.user("Current"))
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
        usage=None,
        run=run,
    )

    generic_tokens, _ = estimate_request_input_tokens(messages)
    assert generic_tokens > 95
    assert compaction_service.should_auto_calls == [(95, 100, 0.8)]


@pytest.mark.asyncio
async def test_compaction_records_post_projection_with_selected_wire_estimator(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])

    class SequencedEstimateAdapter(StubAdapter):
        def __init__(self) -> None:
            super().__init__([])
            self.estimates = iter((95, 37))

        def estimate_request_input_tokens(
            self,
            _messages: list[JsonObject],
            *,
            model_id: str,
            tools: list[JsonObject] | None = None,
        ) -> int:
            del model_id, tools
            return next(self.estimates)

    adapter = SequencedEstimateAdapter()
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted snapshot.",
        projection=[ChatMessage.user("Tail")],
        compacted_token_count=50,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
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
    session.append(ChatMessage.user("Head"))
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
        usage=None,
        run=run,
    )

    persisted_checkpoint = session.load()[-1]
    assert persisted_checkpoint.role == "compaction_checkpoint"
    assert persisted_checkpoint.usage is not None
    assert persisted_checkpoint.usage["context_tokens_before"] == 95
    assert persisted_checkpoint.usage["context_tokens_after"] == 37
    started_event = next(event for event in run.events if event.type == COMPACTION_STARTED_EVENT)
    assert started_event.payload["context_tokens_before"] == 95
    assert started_event.payload["context_usage"] == {
        "tokens": 95,
        "estimated": True,
    }


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
    messages = await build_chat_loop(runtime)._requests._build_request_messages(agent, session)
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
    assert compaction_service.should_auto_calls == [(90, 100, 0.8)]
    assert compaction_service.compact_calls == []


@pytest.mark.asyncio
async def test_summary_tail_waits_until_a_loaded_skill_result_is_consumed(tmp_path: Path) -> None:
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
    session.append(ChatMessage.user("Use the document workflow"))
    session.append(
        ChatMessage.assistant(
            model=agent.model,
            content=None,
            tool_calls=[ToolCall(id="call-skill", name="skill", arguments={"name": "docx"})],
        )
    )
    session.append(
        ChatMessage.tool(
            tool_call_id="call-skill",
            name="skill",
            content=json.dumps(
                tool_success({"name": "docx", "status": "loaded", "content": "Instructions"}),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    )
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted after consumption.",
        projection=[ChatMessage.user("Tail")],
        compacted_token_count=20,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._requests._build_request_messages(agent, session)

    first = await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=Run(run_id="run-1", agent_id=agent.id, session_id=session.id),
    )

    assert first == messages
    assert compaction_service.compact_calls == []
    assert compaction_service.compactable_context_calls == []

    session.append(ChatMessage.assistant(model=agent.model, content="Skill result consumed"))
    consumed_messages = await loop._requests._build_request_messages(agent, session)
    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        consumed_messages,
        usage={"input_tokens": 90},
        run=Run(run_id="run-2", agent_id=agent.id, session_id=session.id),
    )

    assert len(compaction_service.compact_calls) == 1


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
    messages = await build_chat_loop(runtime)._requests._build_request_messages(agent, session)
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
