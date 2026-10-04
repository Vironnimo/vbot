"""Requests around Compaction: the summary call's target and what later requests see."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast, override

import pytest

from core.chat import ChatMessage
from core.chat._request_history import _restore_in_run_tool_result_content
from core.compaction import TOOL_RESULT_COMPACTED_FIELD, CompactionService
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.runs import Run
from core.utils.tokens import estimate_request_input_tokens
from tests.core.chat.chat_loop_compaction_test_support import (
    CompactOnceService,
    JsonObject,
    RecordingCompactionAdapter,
    compaction_runtime,
    real_compaction_runtime,
    run_context,
    word_count_tools,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubModels,
    build_chat_loop,
    build_request_messages,
    session_address,
)


class _ContextAdapter(RecordingCompactionAdapter):
    @override
    def request_context_kwargs(self, **context: Any) -> JsonObject:
        return {"_test_context": {**context, "adapter": id(self)}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "manual", "summary_model"),
    [
        ("summary_tail", False, None),
        ("summary_tail", False, "other/summary"),
        ("summary_tail", False, "openai/gpt-5.2"),
        ("summary_tail", True, None),
        ("summary_tail", True, "other/summary"),
        ("summary_tail", True, "openai/gpt-5.2"),
        ("continuation", False, None),
        ("continuation", True, None),
    ],
    ids=[
        "automatic-summary-tail-active-target",
        "automatic-summary-tail-summary-target",
        "automatic-summary-tail-summary-model-names-active-target",
        "manual-summary-tail-active-target",
        "manual-summary-tail-summary-target",
        "manual-summary-tail-summary-model-names-active-target",
        "automatic-continuation",
        "manual-continuation",
    ],
)
async def test_compaction_routes_session_context_through_selected_adapter(
    tmp_path: Path, strategy: str, manual: bool, summary_model: str | None
) -> None:
    separate_summary = summary_model == "other/summary"
    active = _ContextAdapter(
        [{"content": "finished", "tool_calls": None}], summaries=["ACTIVE SUMMARY"]
    )
    summary = _ContextAdapter([], summaries=["SEPARATE SUMMARY"])
    strategy_settings: JsonObject = {"type": strategy}
    if strategy == "summary_tail":
        strategy_settings.update(tail_tokens=100, summary_model=summary_model)
    runtime = real_compaction_runtime(
        tmp_path,
        active,
        {
            "enabled": True,
            "trigger": {"type": "input_tokens", "tokens": 10_000},
            "strategy": strategy_settings,
        },
        provider_ids={"openai", "other"},
        adapters_by_connection={"other:api-key": summary},
        models=StubModels({("openai", "gpt-5.2"): 1_000_000, ("other", "summary"): 1_000_000}),
    )
    session = runtime.chat_sessions.create("coder", session_id="child-session")
    session.append(ChatMessage.user("old context " * 8_000))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="old response " * 8_000))
    session.append(ChatMessage.user("recent request"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="recent answer"))
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

    # The Engine asks the selected target's own Adapter for request context. A
    # summary Model naming the active Model is the active target. On the active
    # target the summary call keeps the Agent's reasoning setting, which shapes
    # the rendered prompt; another target gets the Provider default.
    selected, other = (summary, active) if separate_summary else (active, summary)
    [compaction_request] = selected.stream_requests
    assert compaction_request["kwargs"]["_test_context"] == {
        "agent_id": "coder",
        "session_id": session.id,
        "project_id": None,
        "prompt_cache_affinity_id": affinity,
        "adapter": id(selected),
    }
    assert compaction_request["kwargs"]["thinking_effort"] == ("" if separate_summary else "high")
    assert other.stream_requests == []
    assert sum(message.role == "compaction_checkpoint" for message in session.load()) == 1
    # The checkpoint keeps the affinity, so the Provider routes the next request
    # to the cache holding the unchanged System Prompt and Tool prefix.
    assert runtime.chat_sessions.prompt_cache_affinity_id(session.address) == affinity
    if not manual:
        assert active.requests[0]["kwargs"]["_test_context"]["prompt_cache_affinity_id"] == affinity


@pytest.mark.asyncio
async def test_request_after_checkpoints_holds_only_the_latest_summary_and_its_tail(
    tmp_path: Path,
) -> None:
    runtime = compaction_runtime(tmp_path)
    agent = runtime.agents.get("coder")
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    old_question = ChatMessage.user("Old question")
    session.append(old_question)
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Superseded summary.", projection=[old_question], compacted_token_count=10
        )
    )
    tail = [
        ChatMessage.user("Tail question"),
        ChatMessage.assistant(model=agent.model, content="Tail answer"),
    ]
    session.append_many(tail)
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted historical context.", projection=tail, compacted_token_count=123
        )
    )

    request = await build_request_messages(build_chat_loop(runtime), agent, session)

    assert [message["role"] for message in request] == ["system", "user", "user", "assistant"]
    assert request[1]["content"] == (
        "<system-reminder>\nCompacted historical context.\n</system-reminder>"
    )
    assert [message["content"] for message in request[2:]] == ["Tail question", "Tail answer"]
    request_text = json.dumps(request)
    assert "Old question" not in request_text and "Superseded summary." not in request_text


@pytest.mark.asyncio
async def test_rebuilt_request_never_restores_rich_content_for_an_aged_tool_result() -> None:
    aged_content = json.dumps(
        {
            TOOL_RESULT_COMPACTED_FIELD: True,
            "tool": "read",
            "original_chars": 50_000,
            "outcome": {"ok": True},
        }
    )
    rebuilt = [
        {"id": "msg_image", "role": "tool", "tool_call_id": "call-image", "content": aged_content}
    ]
    live = [
        {
            "id": "msg_image",
            "role": "tool",
            "tool_call_id": "call-image",
            "content": '{"ok":true}',
            TOOL_RESULT_CONTENT_BLOCKS_FIELD: [{"type": "text", "text": "rich"}],
        }
    ]

    restored = await _restore_in_run_tool_result_content(rebuilt, live)

    assert TOOL_RESULT_CONTENT_BLOCKS_FIELD not in restored[0]


@pytest.mark.asyncio
async def test_rebuilt_request_restores_each_turns_media_when_tool_call_ids_repeat() -> None:
    # Ollama and id-less streams name the first call of every response tool_call_0.
    live = [
        {
            "id": f"msg_result_{turn}",
            "role": "tool",
            "tool_call_id": "tool_call_0",
            "content": '{"ok":true}',
            TOOL_RESULT_CONTENT_BLOCKS_FIELD: [{"type": "text", "text": f"turn-{turn}"}],
        }
        for turn in (1, 2)
    ]
    rebuilt = [
        {key: value for key, value in message.items() if key != TOOL_RESULT_CONTENT_BLOCKS_FIELD}
        for message in live
    ]

    restored = await _restore_in_run_tool_result_content(rebuilt, live)

    assert [message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] for message in restored] == [
        [{"type": "text", "text": "turn-1"}],
        [{"type": "text", "text": "turn-2"}],
    ]


@pytest.mark.asyncio
async def test_final_answer_checkpoint_keeps_the_tool_list_of_the_next_run(tmp_path: Path) -> None:
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
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["word_count"])
    runtime = compaction_runtime(tmp_path, agent=agent, adapter=adapter, tools=word_count_tools())
    loop = build_chat_loop(runtime, compaction_service=cast(Any, CompactOnceService(keep_last=2)))

    await loop.send("coder", "First", session_id="session-one")

    session = runtime.chat_sessions.get(session_address("coder", "session-one"))
    checkpoint = next(
        message for message in session.load() if message.role == "compaction_checkpoint"
    )
    probe = Run(run_id="probe", agent_id="coder", session_id="session-one")
    next_request = (await run_context(loop, probe, session)).request_state
    # The stored after-count covers the next request on the Run's route, including
    # the Tail's replayed reasoning and its Tool definitions.
    tokens_after, _ = estimate_request_input_tokens(next_request.messages, next_request.tools)
    messages_only_tokens, _ = estimate_request_input_tokens(next_request.messages)
    assert checkpoint.usage is not None
    assert checkpoint.usage["context_tokens_after"] == tokens_after > messages_only_tokens

    await loop.send("coder", "Second", session_id="session-one")

    # Compaction leaves the Tool list, and with it the Provider prompt cache prefix, unchanged.
    tool_names = [
        [tool["name"] for tool in request["kwargs"]["tools"]] for request in adapter.requests
    ]
    assert tool_names == [["word_count"], ["word_count"]]


@pytest.mark.asyncio
async def test_next_run_continues_the_compacting_runs_request_byte_for_byte(
    tmp_path: Path,
) -> None:
    usage = {"input_tokens": 50_000, "output_tokens": 10}
    adapter = RecordingCompactionAdapter(
        [
            {
                "content": None,
                "reasoning": "TAIL_PLAN",
                "reasoning_meta": {"encrypted_content": "opaque-tail"},
                "usage": usage,
                "tool_calls": [
                    {"id": "call-one", "name": "word_count", "arguments": {"text": "a"}}
                ],
            },
            {"content": "FIRST_DONE", "usage": {"input_tokens": 10}, "tool_calls": None},
            {"content": "SECOND_DONE", "usage": {"input_tokens": 10}, "tool_calls": None},
        ],
        summaries=["SUMMARY"],
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
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD_CONTEXT " + ("old context " * 5_000)))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="old answer " * 5_000))
    loop = build_chat_loop(runtime, compaction_service=CompactionService())

    await loop.send("coder", "First", session_id=session.id)
    await loop.send("coder", "Second", session_id=session.id)

    def sent(request: JsonObject) -> list[tuple[Any, ...]]:
        return [
            tuple(message.get(key) for key in ("role", "content", "reasoning", "reasoning_meta"))
            + (json.dumps(message.get("tool_calls"), sort_keys=True), message.get("tool_call_id"))
            for message in request["messages"]
        ]

    assert adapter.events == ["agent", "compaction", "agent", "agent"]
    _, after_compaction, next_run = (sent(request) for request in adapter.requests)
    # The Tail keeps its native reasoning in the stored checkpoint, so the next
    # Run resends the compacting Run's post-Compaction request unchanged.
    assert ("assistant", None, "TAIL_PLAN", {"encrypted_content": "opaque-tail"}) in [
        entry[:4] for entry in after_compaction
    ]
    assert next_run[: len(after_compaction)] == after_compaction
