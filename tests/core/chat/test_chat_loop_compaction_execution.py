"""Tests for chat loop compaction execution."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
)
from core.chat._run_state import (
    RequestBuildInputs,
    _RunRequest,
    create_run_execution_context,
)
from core.chat.messages import HISTORY_COMPACTION_GUIDANCE
from core.compaction import (
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    CompactionService,
)
from core.compaction.compaction import (
    COMPACTION_SUMMARY_END_MARKER,
)
from core.runs import (
    COMPACTION_COMPLETED_EVENT,
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
    _maybe_auto_compact,
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
    session_address,
)


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
    compaction_service = StubCompactionService(
        should_auto=True,
        checkpoint=checkpoint,
    )
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._requests._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
    affinity_before = runtime.chat_sessions.prompt_cache_affinity_id(
        session_address("coder", session.id)
    )

    compaction_logger = logging.getLogger("vbot.compaction.coordination")
    compaction_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level("INFO", logger=compaction_logger.name):
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
    finally:
        compaction_logger.removeHandler(caplog.handler)

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
    assert (
        runtime.chat_sessions.prompt_cache_affinity_id(session_address("coder", session.id))
        != affinity_before
    )
    assert len(compaction_service.compact_calls) == 1
    assert compaction_service.compact_calls[0]["summary_model_id"] == "gpt-5.2"
    assert compaction_service.compact_calls[0]["summary_adapter"] is adapter
    assert compaction_service.compact_calls[0]["request_messages"] == messages
    assert compaction_service.compact_calls[0]["summary_temperature"] is None
    assert compaction_service.compact_calls[0]["active_temperature"] is None
    assert (
        compaction_service.compact_calls[0]["minimum_reclaim_tokens"]
        == MIN_AUTO_COMPACTION_RECLAIM_TOKENS
    )
    assert [message["role"] for message in rebuilt] == ["system", "user", "user", "assistant"]
    reminder = rebuilt[1]["content"]
    assert reminder.startswith("<system-reminder>\n")
    assert reminder.endswith("\n</system-reminder>")
    assert "Compacted tail context." in reminder
    assert rebuilt[2]["content"] == "Tail user"
    assert rebuilt[3]["content"] == "Tail assistant"
    post_compaction_tools = runtime.system_prompts.provider_tool_definitions(
        agent,
        session_tool_grants=(HISTORY_TOOL_NAME,),
    )
    expected_context_tokens_after, _ = estimate_request_input_tokens(
        rebuilt,
        post_compaction_tools,
    )
    compaction_events = [
        event
        for event in run.events
        if event.type in {COMPACTION_STARTED_EVENT, COMPACTION_COMPLETED_EVENT}
    ]
    assert [event.type for event in compaction_events] == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_COMPLETED_EVENT,
    ]
    assert compaction_events[0].payload == {
        "context_tokens_before": 90,
        "context_usage": {
            "tokens": 90,
            "estimated": False,
            "provider_input_tokens": 90,
        },
    }
    compaction_event = next(
        event for event in run.events if event.type == COMPACTION_COMPLETED_EVENT
    )
    assert compaction_event.payload["checkpoint"] == 1
    assert compaction_event.payload["checkpoint_id"] == checkpoint.id
    assert compaction_event.payload["history_available"] is True
    assert compaction_event.payload["context_tokens_before"] == 90
    assert compaction_event.payload["context_tokens_after"] == expected_context_tokens_after
    assert compaction_event.payload["context_usage"] == {
        "tokens": expected_context_tokens_after,
        "estimated": True,
    }
    final_usage = dict(session.load()[-1].usage or {})
    compaction_duration_ms = final_usage.pop("compaction_duration_ms")
    assert final_usage == {
        "compacted_token_count": 42,
        "context_tokens_before": 90,
        "context_tokens_after": expected_context_tokens_after,
    }
    assert isinstance(compaction_duration_ms, int)
    assert compaction_duration_ms >= 0


@pytest.mark.asyncio
async def test_compaction_resolves_model_recommended_temperatures_for_both_targets(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="ollama-cloud/glm-5.2", allowed_tools=["*"])
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
                "summary_model": "ollama-cloud/qwen3",
            }
        ),
        models=StubModels(
            {("ollama-cloud", "glm-5.2"): 100, ("ollama-cloud", "qwen3"): 100},
            recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
        ),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Tail user"))
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
        "glm-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    assert compaction_service.compact_calls[0]["summary_temperature"] is None
    assert compaction_service.compact_calls[0]["active_temperature"] == 1.0


@pytest.mark.asyncio
async def test_compaction_allows_repeated_automatic_checkpoints_in_one_run(
    tmp_path: Path,
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
    tail_user = ChatMessage.user("Tail user")
    tail_assistant = ChatMessage.assistant(model=agent.model, content="Tail assistant")
    session.append(tail_user)
    session.append(tail_assistant)
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=[tail_user, tail_assistant],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
    context = await create_run_execution_context(
        loop._dependencies,
        loop._requests,
        run,
        _RunRequest(content="test"),
        session=session,
        prior_continuation=None,
        continuation_reminder=None,
        continuation_tracker=None,
    )
    context.request_state = await loop._requests.build_request_state(
        agent,
        session,
        inputs=RequestBuildInputs.from_context(context, context.primary_target),
    )

    requested_compactions = 5
    affinity_epochs = [context.prompt_cache_affinity_id]
    for _ in range(requested_compactions):
        context.request_state = await loop._compaction_runs.maybe_auto_compact_state(
            context,
            context.primary_target,
            usage={"input_tokens": 90},
        )
        affinity_epochs.append(context.prompt_cache_affinity_id)

    assert len(compaction_service.compact_calls) == requested_compactions
    assert persisted_roles(session.load()).count("compaction_checkpoint") == requested_compactions
    assert len(set(affinity_epochs)) == requested_compactions + 1
    assert context.prompt_cache_affinity_id == runtime.chat_sessions.prompt_cache_affinity_id(
        session_address("coder", session.id)
    )


@pytest.mark.asyncio
async def test_real_compaction_repeats_between_complete_tool_iterations(
    tmp_path: Path,
) -> None:
    current_user = "CURRENT_USER_MARKER: continue the agreed work"
    first_payload = "FIRST_TOOL_PAYLOAD " + ("alpha " * 8_000)
    second_payload = "SECOND_TOOL_PAYLOAD " + ("beta " * 8_000)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["word_count"])
    adapter = _RealCompactionAdapter(
        [
            {
                "content": None,
                "usage": {"input_tokens": 50_000, "output_tokens": 10},
                "tool_calls": [
                    {
                        "id": "call-one",
                        "name": "word_count",
                        "arguments": {"text": first_payload},
                    }
                ],
            },
            {
                "content": None,
                "usage": {"input_tokens": 50_000, "output_tokens": 10},
                "tool_calls": [
                    {
                        "id": "call-two",
                        "name": "word_count",
                        "arguments": {"text": second_payload},
                    }
                ],
            },
            {
                "content": "AUTO_DONE",
                "usage": {"input_tokens": 50_000, "output_tokens": 2},
                "tool_calls": None,
            },
        ],
        summaries=["SUMMARY ONE", "SUMMARY TWO", "SUMMARY THREE"],
    )
    tools = ToolRegistry()
    tools.register(
        "word_count",
        "Count words.",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        lambda _context, arguments: tool_success({"words": len(str(arguments["text"]).split())}),
    )
    storage = _RealCompactionStorage(
        {
            "enabled": True,
            "trigger": {"type": "input_tokens", "tokens": 40_000},
            "strategy": {
                "type": "summary_tail",
                "tail_tokens": 1_000,
                "summary_model": None,
            },
        },
        data_dir=tmp_path,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
        storage=storage,
        models=StubModels({("openai", "gpt-5.2"): 1_000_000}),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD_CONTEXT_MARKER " + ("old context " * 5_000)))
    session.append(ChatMessage.assistant(model=agent.model, content="old answer " * 5_000))

    assistant = await build_chat_loop(
        runtime,
        compaction_service=CompactionService(),
    ).send("coder", current_user, session_id=session.id)

    persisted = session.load()
    checkpoints = [message for message in persisted if message.role == "compaction_checkpoint"]
    assert assistant.content == "AUTO_DONE"
    assert adapter.events == [
        "agent",
        "compaction",
        "agent",
        "compaction",
        "agent",
        "compaction",
    ]
    assert storage.prompt_fragment_reads == ["compaction.md"] * 3
    assert len(checkpoints) == 3
    assert persisted_roles(persisted)[-8:] == [
        "assistant",
        "tool",
        "compaction_checkpoint",
        "assistant",
        "tool",
        "compaction_checkpoint",
        "assistant",
        "compaction_checkpoint",
    ]

    for ordinal, checkpoint_message in enumerate(checkpoints, start=1):
        projection = checkpoint_message.projection
        assert projection is not None
        assert (
            sum(
                COMPACTION_SUMMARY_END_MARKER in str(message.get("content") or "")
                for message in projection
            )
            == 1
        )
        summary_content = next(
            str(message.get("content") or "")
            for message in projection
            if COMPACTION_SUMMARY_END_MARKER in str(message.get("content") or "")
        )
        assert summary_content.endswith(COMPACTION_SUMMARY_END_MARKER)
        assert summary_content.index(HISTORY_COMPACTION_GUIDANCE.format(ordinal=ordinal)) < (
            summary_content.index(COMPACTION_SUMMARY_END_MARKER)
        )
        for index, message in enumerate(projection):
            if message["role"] != "tool":
                continue
            carrier = projection[index - 1]
            assert carrier["role"] == "assistant"
            assert message["tool_call_id"] in {call["id"] for call in carrier["tool_calls"]}

    compaction_requests = [json.dumps(call["messages"]) for call in adapter.stream_requests]
    assert all("<retained_tail>" not in request for request in compaction_requests)
    assert "CURRENT_USER_MARKER" in compaction_requests[0]
    assert "FIRST_TOOL_PAYLOAD" not in compaction_requests[0]
    assert "FIRST_TOOL_PAYLOAD" in compaction_requests[1]
    assert "SECOND_TOOL_PAYLOAD" not in compaction_requests[1]
    assert "SECOND_TOOL_PAYLOAD" in compaction_requests[2]

    third_agent_request = json.dumps(adapter.requests[2]["messages"])
    assert "CURRENT_USER_MARKER" in third_agent_request
    assert "SECOND_TOOL_PAYLOAD" in third_agent_request
    assert [message["role"] for message in adapter.requests[2]["messages"]][-3:] == [
        "user",
        "assistant",
        "tool",
    ]
    final_projection = json.dumps(checkpoints[-1].projection)
    assert "SUMMARY THREE" in final_projection
    assert "SUMMARY ONE" not in final_projection
    assert "SUMMARY TWO" not in final_projection


@pytest.mark.asyncio
async def test_continuation_compacts_before_first_request_and_after_complete_tool_results(
    tmp_path: Path,
) -> None:
    current_user = "CURRENT_CONTINUATION_USER " + ("current work " * 5_000)
    tool_payload = "alpha beta " * 5_000
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["word_count"])
    adapter = _RealCompactionAdapter(
        [
            {
                "content": None,
                "usage": {"input_tokens": 50_000, "output_tokens": 10},
                "tool_calls": [
                    {
                        "id": "call-one",
                        "name": "word_count",
                        "arguments": {"text": tool_payload},
                    }
                ],
            },
            {
                "content": "CONTINUATION_DONE",
                "usage": {"input_tokens": 50_000, "output_tokens": 2},
                "tool_calls": None,
            },
        ],
        summaries=["PREFLIGHT CHECKPOINT", "TOOL CHECKPOINT"],
    )
    tools = ToolRegistry()
    tools.register(
        "word_count",
        "Count words.",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        lambda _context, arguments: tool_success({"words": len(str(arguments["text"]).split())}),
    )
    storage = _RealCompactionStorage(
        {
            "enabled": True,
            "trigger": {"type": "input_tokens", "tokens": 1},
            "strategy": {"type": "continuation"},
        },
        data_dir=tmp_path,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
        storage=storage,
        models=StubModels({("openai", "gpt-5.2"): 1_000_000}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD CONTEXT " + ("old work " * 5_000)))
    session.append(ChatMessage.assistant(model=agent.model, content="old answer " * 5_000))

    assistant = await build_chat_loop(
        runtime,
        compaction_service=CompactionService(),
    ).send("coder", current_user, session_id=session.id)

    assert assistant.content == "CONTINUATION_DONE"
    assert adapter.events == ["compaction", "agent", "compaction", "agent"]
    assert storage.prompt_fragment_reads == ["compaction-continuation.md"] * 2
    assert len(adapter.stream_requests) == 2
    assert current_user in json.dumps(adapter.stream_requests[0]["messages"])
    second_compaction_roles = [
        message["role"] for message in adapter.stream_requests[1]["messages"]
    ]
    assert second_compaction_roles[-3:] == ["assistant", "tool", "user"]
    checkpoints = [message for message in session.load() if message.role == "compaction_checkpoint"]
    assert [message.content for message in checkpoints] == [
        "PREFLIGHT CHECKPOINT",
        "TOOL CHECKPOINT",
    ]
