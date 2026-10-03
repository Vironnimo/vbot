"""Automatic and user-requested Compaction at the boundaries of a running Agentic Run."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast, override

import pytest

from core.chat import ChatMessage
from core.chat.messages import HISTORY_COMPACTION_GUIDANCE
from core.compaction import MIN_AUTO_COMPACTION_RECLAIM_TOKENS, CompactionService
from core.compaction.compaction import COMPACTION_SUMMARY_END_MARKER
from core.runs import COMPACTION_ABORTED_EVENT, COMPACTION_COMPLETED_EVENT, COMPACTION_STARTED_EVENT
from core.tools import HISTORY_TOOL_NAME, ToolRegistry, register_history_tool, tool_success
from core.utils.tokens import estimate_request_input_tokens
from tests.core.chat.chat_loop_compaction_test_support import (
    WAIT_SECONDS,
    JsonObject,
    RecordingCompactionAdapter,
    auto_compact,
    compaction_runtime,
    real_compaction_runtime,
    seed_tail,
    word_count_tools,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubModels,
    build_chat_loop,
    persisted_roles,
)


class _RecordingCompactionService(StubCompactionService):
    """Compact at every boundary and record the Session history each attempt received."""

    def __init__(self) -> None:
        super().__init__(should_auto=True)
        self.compacted_contents: list[list[Any]] = []

    @override
    async def compact(self, messages: list[ChatMessage], **_kwargs: Any) -> ChatMessage:
        self.compacted_contents.append([message.content for message in messages])
        return ChatMessage.compaction_checkpoint(
            summary=f"SUMMARY {len(self.compacted_contents)}",
            projection=[],
            compacted_token_count=10,
        )


class _AffinityAdapter(StubAdapter):
    @override
    def request_context_kwargs(self, **context: Any) -> JsonObject:
        return {"_test_context": context}


def _probe_tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        "probe",
        "Return a fixed value.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"value": 1}),
    )
    return tools


@pytest.mark.asyncio
async def test_automatic_compaction_commits_a_checkpoint_and_rebuilds_the_request(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = compaction_runtime(tmp_path)
    agent = runtime.agents.get("coder")
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    checkpoint = seed_tail(session)
    service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    affinity_before = runtime.chat_sessions.prompt_cache_affinity_id(session.address)

    caplog.set_level(logging.INFO, logger="vbot.compaction.coordination")
    probe = await auto_compact(loop, agent, session, usage={"input_tokens": 90})

    # One INFO line reports the committed Compaction; per-step detail stays at DEBUG.
    [completed] = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.compaction.coordination" and record.levelno >= logging.INFO
    ]
    fields = ("session=session-one", "trigger=auto", "reason=context_ratio", "input_tokens=90")
    assert all(field in completed for field in fields)
    assert all(key in completed for key in ("tokens_after=", "carried_notes=0", "duration_ms="))
    assert persisted_roles(session.load()) == ["user", "assistant", "compaction_checkpoint"]
    assert runtime.chat_sessions.prompt_cache_affinity_id(session.address) != affinity_before
    [call] = service.compact_calls
    assert call["summary_model_id"] == "gpt-5.2"
    assert call["summary_adapter"] is runtime.adapter
    assert call["request_messages"] == probe.request
    assert (call["summary_temperature"], call["active_temperature"]) == (None, None)
    assert call["minimum_reclaim_tokens"] == MIN_AUTO_COMPACTION_RECLAIM_TOKENS

    # The rebuilt request is the summary reminder followed by the native Tail.
    assert [message["role"] for message in probe.rebuilt] == ["system", "user", "user", "assistant"]
    reminder = probe.rebuilt[1]["content"]
    assert reminder.startswith("<system-reminder>\n") and reminder.endswith("\n</system-reminder>")
    assert "Compacted tail context." in reminder
    assert [message["content"] for message in probe.rebuilt[2:]] == ["Tail user", "Tail assistant"]

    # The after-count estimates the rebuilt request with the now granted history Tool.
    tokens_after, _ = estimate_request_input_tokens(
        probe.rebuilt,
        runtime.system_prompts.provider_tool_definitions(
            agent, session_tool_grants=(HISTORY_TOOL_NAME,)
        ),
    )
    lifecycle = [
        event
        for event in probe.run.events
        if event.type in {COMPACTION_STARTED_EVENT, COMPACTION_COMPLETED_EVENT}
    ]
    assert [event.type for event in lifecycle] == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_COMPLETED_EVENT,
    ]
    assert lifecycle[0].payload == {
        "context_tokens_before": 90,
        "context_usage": {
            "tokens": 90,
            "estimated": False,
            "provider_input_tokens": 90,
            "context_window": 100,
        },
    }
    assert lifecycle[1].payload["checkpoint"] == 1
    assert lifecycle[1].payload["checkpoint_id"] == checkpoint.id
    assert lifecycle[1].payload["history_available"] is True
    assert lifecycle[1].payload["context_tokens_before"] == 90
    assert lifecycle[1].payload["context_tokens_after"] == tokens_after
    assert lifecycle[1].payload["context_usage"] == {"tokens": tokens_after, "estimated": True}
    usage = dict(session.load()[-1].usage or {})
    duration_ms = usage.pop("compaction_duration_ms")
    assert usage == {
        "compacted_token_count": 42,
        "context_tokens_before": 90,
        "context_tokens_after": tokens_after,
    }
    assert isinstance(duration_ms, int) and duration_ms >= 0
    assert lifecycle[1].payload["duration_ms"] == duration_ms
    assert lifecycle[1].payload["message"]["usage"]["compaction_duration_ms"] == duration_ms


@pytest.mark.asyncio
async def test_compaction_resolves_model_recommended_temperatures_for_both_targets(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="ollama-cloud/glm-5.2", allowed_tools=["*"])
    runtime = compaction_runtime(
        tmp_path,
        agent=agent,
        settings={"summary_model": "ollama-cloud/qwen3"},
        models=StubModels(
            {("ollama-cloud", "glm-5.2"): 100, ("ollama-cloud", "qwen3"): 100},
            recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
        ),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))

    await auto_compact(
        build_chat_loop(runtime, compaction_service=cast(Any, service)),
        agent,
        session,
        usage={"input_tokens": 90},
    )

    assert service.compact_calls[0]["summary_temperature"] is None
    assert service.compact_calls[0]["active_temperature"] == 1.0


@pytest.mark.asyncio
async def test_automatic_compaction_boundaries_never_reload_complete_history(
    tmp_path: Path,
) -> None:
    adapter = _AffinityAdapter(
        [
            {"content": None, "tool_calls": [{"id": "call-one", "name": "probe", "arguments": {}}]},
            {"content": "done", "tool_calls": None},
        ]
    )
    runtime = compaction_runtime(
        tmp_path, adapter=adapter, tools=_probe_tools(), context_window=1_000_000
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Earlier context"))
    service = _RecordingCompactionService()
    store = runtime.chat_sessions._store
    reads = store.messages_since
    complete_reads: list[None] = []

    def recording_messages_since(address: Any, cursor: Any) -> Any:
        if cursor is None:
            complete_reads.append(None)
        return reads(address, cursor)

    store.messages_since = recording_messages_since
    affinity_before = runtime.chat_sessions.prompt_cache_affinity_id(session.address)

    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    await (await loop.start_run("coder", "Go", session_id="session-one")).wait()

    # Before the first request, after the Tool batch and after the final answer each
    # compacted, yet only the Run-start snapshot read the complete history.
    assert len(service.compacted_contents) == 3
    assert complete_reads == [None]
    assert persisted_roles(session.load_active()).count("compaction_checkpoint") == 3
    # Every checkpoint rotated the prompt-cache affinity the next request carries.
    affinities = [
        affinity_before,
        *(
            request["kwargs"]["_test_context"]["prompt_cache_affinity_id"]
            for request in adapter.requests
        ),
        runtime.chat_sessions.prompt_cache_affinity_id(session.address),
    ]
    assert len(set(affinities)) == 4


@pytest.mark.asyncio
async def test_edit_run_compacts_only_its_edited_lineage(tmp_path: Path) -> None:
    runtime = compaction_runtime(
        tmp_path, adapter=StubAdapter([{"content": "new answer"}]), context_window=1_000_000
    )
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
async def test_real_compaction_repeats_between_complete_tool_iterations(tmp_path: Path) -> None:
    first_payload = "FIRST_TOOL_PAYLOAD " + ("alpha " * 8_000)
    second_payload = "SECOND_TOOL_PAYLOAD " + ("beta " * 8_000)
    usage = {"input_tokens": 50_000, "output_tokens": 10}
    adapter = RecordingCompactionAdapter(
        [
            {
                "content": None,
                "usage": usage,
                "tool_calls": [
                    {"id": "call-one", "name": "word_count", "arguments": {"text": first_payload}}
                ],
            },
            {
                "content": None,
                "usage": usage,
                "tool_calls": [
                    {"id": "call-two", "name": "word_count", "arguments": {"text": second_payload}}
                ],
            },
            {"content": "AUTO_DONE", "usage": usage, "tool_calls": None},
        ],
        summaries=["SUMMARY ONE", "SUMMARY TWO", "SUMMARY THREE"],
    )
    runtime = real_compaction_runtime(
        tmp_path,
        adapter,
        {
            "enabled": True,
            "trigger": {"type": "input_tokens", "tokens": 40_000},
            "strategy": {"type": "summary_tail", "tail_tokens": 1, "summary_model": None},
        },
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["word_count"]),
        tools=word_count_tools(),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD_CONTEXT_MARKER " + ("old context " * 5_000)))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="old answer " * 5_000))

    assistant = await build_chat_loop(runtime, compaction_service=CompactionService()).send(
        "coder", "CURRENT_USER_MARKER: continue the agreed work", session_id=session.id
    )

    persisted = session.load()
    checkpoints = [message for message in persisted if message.role == "compaction_checkpoint"]
    assert assistant.content == "AUTO_DONE"
    assert adapter.events == ["agent", "compaction"] * 3
    assert runtime.storage.prompt_fragment_reads == ["compaction.md"] * 3
    assert persisted_roles(persisted)[-8:] == [
        *("assistant", "tool", "compaction_checkpoint") * 2,
        "assistant",
        "compaction_checkpoint",
    ]
    for ordinal, checkpoint in enumerate(checkpoints, start=1):
        projection = checkpoint.projection
        assert projection is not None
        [summary] = [
            str(message.get("content") or "")
            for message in projection
            if COMPACTION_SUMMARY_END_MARKER in str(message.get("content") or "")
        ]
        assert summary.endswith(COMPACTION_SUMMARY_END_MARKER)
        assert summary.index(HISTORY_COMPACTION_GUIDANCE.format(ordinal=ordinal)) < summary.index(
            COMPACTION_SUMMARY_END_MARKER
        )
        # Tool Results always stay directly behind the Assistant step that called them.
        for index, message in enumerate(projection):
            if message["role"] == "tool":
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
    third_agent_request = adapter.requests[2]["messages"]
    assert "CURRENT_USER_MARKER" in json.dumps(third_agent_request)
    assert "SECOND_TOOL_PAYLOAD" in json.dumps(third_agent_request)
    assert [message["role"] for message in third_agent_request][-3:] == [
        "user",
        "assistant",
        "tool",
    ]
    final_projection = json.dumps(checkpoints[-1].projection)
    assert "SUMMARY THREE" in final_projection
    assert "SUMMARY ONE" not in final_projection and "SUMMARY TWO" not in final_projection


_CONTINUATION_POLICY: JsonObject = {
    "enabled": True,
    "trigger": {"type": "input_tokens", "tokens": 1},
    "strategy": {"type": "continuation"},
}


@pytest.mark.asyncio
async def test_continuation_compacts_before_first_request_and_after_complete_tool_results(
    tmp_path: Path,
) -> None:
    current_user = "CURRENT_CONTINUATION_USER " + ("current work " * 5_000)
    usage = {"input_tokens": 50_000, "output_tokens": 10}
    adapter = RecordingCompactionAdapter(
        [
            {
                "content": None,
                "usage": usage,
                "tool_calls": [
                    {
                        "id": "call-one",
                        "name": "word_count",
                        "arguments": {"text": "alpha beta " * 5_000},
                    }
                ],
            },
            {"content": "CONTINUATION_DONE", "usage": usage, "tool_calls": None},
        ],
        summaries=["PREFLIGHT CHECKPOINT", "TOOL CHECKPOINT"],
    )
    runtime = real_compaction_runtime(
        tmp_path,
        adapter,
        _CONTINUATION_POLICY,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["word_count"]),
        tools=word_count_tools(),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD CONTEXT " + ("old work " * 5_000)))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="old answer " * 5_000))

    assistant = await build_chat_loop(runtime, compaction_service=CompactionService()).send(
        "coder", current_user, session_id=session.id
    )

    # No final-response check: no work remains in the Run to continue from it.
    assert assistant.content == "CONTINUATION_DONE"
    assert adapter.events == ["compaction", "agent", "compaction", "agent"]
    assert runtime.storage.prompt_fragment_reads == ["compaction-continuation.md"] * 2
    assert current_user in json.dumps(adapter.stream_requests[0]["messages"])
    second_compaction_roles = [
        message["role"] for message in adapter.stream_requests[1]["messages"]
    ]
    assert second_compaction_roles[-3:] == ["assistant", "tool", "user"]
    assert [
        message.content for message in session.load() if message.role == "compaction_checkpoint"
    ] == ["PREFLIGHT CHECKPOINT", "TOOL CHECKPOINT"]


@pytest.mark.asyncio
async def test_continuation_skips_model_call_without_reclaimable_new_context(
    tmp_path: Path,
) -> None:
    # Fixed overhead keeps the request above the trigger after a Continuation
    # checkpoint. A small Tool batch cannot reclaim the minimum, so the next
    # boundary must not pay for a Continuation call the reclaim floor discards.
    usage = {"input_tokens": 50_000, "output_tokens": 10}
    adapter = RecordingCompactionAdapter(
        [
            {
                "content": None,
                "usage": usage,
                "tool_calls": [
                    {"id": "call-one", "name": "word_count", "arguments": {"text": "a b"}}
                ],
            },
            {"content": "CONTINUATION_DONE", "usage": usage, "tool_calls": None},
        ],
        summaries=["PREFLIGHT CHECKPOINT", "UNEXPECTED CHECKPOINT"],
    )
    runtime = real_compaction_runtime(
        tmp_path,
        adapter,
        _CONTINUATION_POLICY,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["word_count"]),
        tools=word_count_tools(),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD CONTEXT " + ("old work " * 5_000)))

    assistant = await build_chat_loop(runtime, compaction_service=CompactionService()).send(
        "coder", "Count the words", session_id=session.id
    )

    assert assistant.content == "CONTINUATION_DONE"
    assert adapter.events == ["compaction", "agent", "agent"]
    assert [
        message.content for message in session.load() if message.role == "compaction_checkpoint"
    ] == ["PREFLIGHT CHECKPOINT"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True], ids=["completed", "failed"])
async def test_user_compaction_waits_for_tool_result_and_continues_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: bool
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_context: Any, _arguments: Any) -> Any:
        started.set()
        await release.wait()
        return tool_success({"done": True})

    tools = ToolRegistry()
    tools.register(
        "wait_test",
        "Test sentinel",
        {"type": "object", "properties": {}, "additionalProperties": False},
        handler,
    )
    runtime = compaction_runtime(
        tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["wait_test"]),
        adapter=StubAdapter(
            [
                {
                    "content": None,
                    "tool_calls": [{"id": "call-one", "name": "wait_test", "arguments": {}}],
                },
                {"content": "finished", "tool_calls": None},
            ]
        ),
        tools=tools,
        settings={"auto": False, "threshold": 0.99},
        context_window=1_000_000,
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Earlier context"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Earlier answer"))
    service = StubCompactionService(
        should_auto=False,
        checkpoint=ChatMessage.compaction_checkpoint(
            summary="SUMMARY_SENTINEL", projection=[], compacted_token_count=8000
        ),
        compact_error=RuntimeError("test failure") if failure else None,
    )
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    caplog.set_level(logging.INFO, logger="vbot.compaction.coordination")

    run = await loop.start_run("coder", "Continue", session_id=session.id)
    await asyncio.wait_for(started.wait(), WAIT_SECONDS)
    # Requests coalesce while pending and wait for the complete Tool batch.
    assert run.request_compaction()
    assert run.request_compaction()
    assert run.compaction_state == "pending"
    assert service.compact_calls == []
    release.set()
    result = await asyncio.wait_for(run.wait(), WAIT_SECONDS)

    assert result.content == "finished"
    [call] = service.compact_calls
    assert call["message_roles"][-2:] == ["assistant", "tool"]
    assert call["minimum_reclaim_tokens"] == MIN_AUTO_COMPACTION_RECLAIM_TOKENS
    assert run.compaction_state == "idle"
    # The one outcome line labels the user request as manual, never as automatic.
    [outcome] = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.compaction.coordination" and record.levelno >= logging.INFO
    ]
    assert "trigger=manual" in outcome
    roles = persisted_roles(session.load())
    event_types = {event.type for event in await runtime.timelines.events(run)}
    if failure:
        assert "compaction_checkpoint" not in roles
        assert COMPACTION_ABORTED_EVENT in event_types
    else:
        assert roles.index("compaction_checkpoint") > roles.index("tool")
        assert COMPACTION_COMPLETED_EVENT in event_types
