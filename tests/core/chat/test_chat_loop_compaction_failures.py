"""Failed, stale and stopped automatic Compaction attempts."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatMessage
from core.chat._message_history import effective_compaction_messages
from core.chat._run_state import RequestBuildInputs, _RequestState
from core.compaction import CompactionError, CompactionService
from core.compaction.run_coordination import AUTO_COMPACTION_COMMIT_ATTEMPTS
from core.prompts.pinned_context import PINNED_SKILL_CATALOG_SLOT, pinned_skill_catalog
from core.providers.errors import NetworkError
from core.runs import (
    COMPACTION_ABORTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
    COMPACTION_STARTED_EVENT,
    RunCancelledError,
    RunStatus,
)
from core.tools import HISTORY_TOOL_NAME, ToolRegistry, register_history_tool, tool_success
from tests.core.chat.chat_loop_compaction_test_support import (
    WAIT_SECONDS,
    CompactionPromptStorage,
    CompactOnceService,
    append_while_compacting,
    auto_compact,
    compaction_runtime,
    seed_tail,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubModels,
    StubSkill,
    StubSkills,
    build_chat_loop,
    build_request_messages,
    persisted_roles,
    session_address,
)

_LIFECYCLE = {COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT}


def _lifecycle(events: list[Any]) -> list[str]:
    return [event.type for event in events if event.type in _LIFECYCLE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("summary_model", "failing_connection"),
    [
        ("malformed-summary-model", None),
        ("missing-provider/gpt-5.2::api-key", "missing-provider:api-key"),
    ],
    ids=["malformed-reference", "unresolvable-connection"],
)
async def test_unusable_summary_model_falls_back_to_the_active_target(
    tmp_path: Path, summary_model: str, failing_connection: str | None
) -> None:
    runtime = compaction_runtime(
        tmp_path,
        settings={"summary_model": summary_model},
        raise_on_connection=(
            {failing_connection: KeyError(failing_connection)} if failing_connection else None
        ),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))

    await auto_compact(
        build_chat_loop(runtime, compaction_service=cast(Any, service)),
        runtime.agents.get("coder"),
        session,
        usage={"input_tokens": 90},
    )

    if failing_connection:
        assert runtime.adapter_connection_id == failing_connection
    [call] = service.compact_calls
    assert call["summary_model_id"] == "gpt-5.2"
    assert call["summary_adapter"] is runtime.adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failure", [True, False], ids=["provider-failure", "defect"])
async def test_failed_compaction_logs_and_keeps_the_request(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, provider_failure: bool
) -> None:
    runtime = compaction_runtime(tmp_path)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Hello"))
    error: Exception = RuntimeError("broke")
    if provider_failure:
        error = CompactionError("Compaction failed: stream dropped", model="openai/summary")
        error.__cause__ = NetworkError("stream dropped")
    service = StubCompactionService(should_auto=True, compact_error=error)

    with caplog.at_level("WARNING", logger="vbot.compaction.coordination"):
        probe = await auto_compact(
            build_chat_loop(runtime, compaction_service=cast(Any, service)),
            runtime.agents.get("coder"),
            session,
            usage={"input_tokens": 90},
        )

    assert probe.rebuilt == probe.request
    assert persisted_roles(session.load()) == ["user", "assistant"]
    # An expected Provider failure is one traceback-free WARNING naming its
    # context and cause; a defect keeps its traceback at ERROR.
    [record] = [
        record for record in caplog.records if record.name == "vbot.compaction.coordination"
    ]
    if provider_failure:
        assert (record.levelno, record.exc_info) == (logging.WARNING, None)
        fields = ("run=run-1", "session=session-one", "model=openai/summary", "NetworkError")
        assert all(field in record.getMessage() for field in fields)
    else:
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None
    assert _lifecycle(probe.run.events) == [COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT]
    assert probe.run.events[-1].payload == {"reason": "failed"}


@pytest.mark.asyncio
async def test_failed_auto_compaction_retries_at_the_next_boundary(tmp_path: Path) -> None:
    compactions_at_tool_boundary: list[int] = []
    service = StubCompactionService(
        should_auto=True, compact_error=RuntimeError("summary unavailable")
    )

    async def probe(_context: Any, _arguments: Any) -> Any:
        compactions_at_tool_boundary.append(len(service.compact_calls))
        return tool_success({"done": True})

    tools = ToolRegistry()
    tools.register("probe", "Probe", {"type": "object"}, probe)
    runtime = compaction_runtime(
        tmp_path,
        adapter=StubAdapter(
            [{"tool_calls": [{"id": "one", "name": "probe", "arguments": {}}]}, {"content": "Done"}]
        ),
        tools=tools,
        context_window=1_000_000,
    )
    session = runtime.chat_sessions.create("coder", session_id="test")
    session.append(ChatMessage.user("Earlier context"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Earlier answer"))
    try:
        result = await build_chat_loop(runtime, compaction_service=cast(Any, service)).send(
            "coder", "Work", session_id="test"
        )
    finally:
        await runtime.chat_runs.aclose()

    # Pre-request check, post-Tool-batch check and final-response check each
    # retried; a failed attempt never suppresses a later eligible boundary.
    assert result.content == "Done"
    assert compactions_at_tool_boundary == [1]
    assert len(service.compact_calls) == 3
    assert "compaction_checkpoint" not in persisted_roles(session.load())


@pytest.mark.asyncio
async def test_truncated_summary_leaves_history_skills_and_prompt_epoch_untouched(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Incomplete summary"},
                {"type": "finish", "reason": "output_truncated"},
            ]
        ],
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"], allowed_skills=["*"])
    runtime = compaction_runtime(
        tmp_path,
        agent=agent,
        adapter=adapter,
        storage=CompactionPromptStorage(
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
    pinned_skill_catalog(
        loop._dependencies, "coder", session.id, agent, runtime.skills, None, skill_project_id=None
    )
    runtime.skills = StubSkills(
        [StubSkill("one", "One.", Path("a")), StubSkill("two", "Two.", Path("b"))]
    )
    original_history = session.load()
    original_skills = session.activated_skill_contents()

    probe = await auto_compact(loop, agent, session, usage=None)

    request_after = await loop._requests.build_request_state(
        agent, session, inputs=RequestBuildInputs()
    )
    catalog_pin = runtime.chat_sessions.prompt_pin(session.address, PINNED_SKILL_CATALOG_SLOT)
    assert probe.rebuilt == probe.request
    assert session.load() == original_history
    assert HISTORY_TOOL_NAME not in request_after.session_tool_grants
    assert session.activated_skill_contents() == original_skills
    assert catalog_pin is not None and catalog_pin["catalog_text"] == "catalog:1"
    assert runtime.refresh_skills_for_calls == []
    assert len(adapter.stream_requests) == 1
    assert _lifecycle(probe.run.events) == [COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT]


@pytest.mark.asyncio
async def test_projected_request_failure_does_not_persist_the_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = compaction_runtime(tmp_path)
    agent = runtime.agents.get("coder")
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    request = await build_request_messages(loop, agent, session)

    async def fail_projected_request(*_args: Any, **_kwargs: Any) -> _RequestState:
        raise RuntimeError("projected request broke")

    monkeypatch.setattr(loop._requests, "build_request_state", fail_projected_request)

    with caplog.at_level("WARNING"):
        probe = await auto_compact(
            loop, agent, session, usage={"input_tokens": 90}, request=request
        )

    assert probe.rebuilt == request
    assert persisted_roles(session.load()) == ["user", "assistant"]
    assert _lifecycle(probe.run.events) == [COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT]
    assert any(
        "Post-compaction request projection failed" in record.message for record in caplog.records
    )


# The Model call runs outside the Session lock, so other writers may commit meanwhile.
# A concurrent note is folded into the checkpoint and stays in the live Context;
# any other entry makes the result stale, so nothing is written.
_CONCURRENT_ENTRIES = [
    pytest.param(ChatMessage.note("Background completed"), True, id="note-carried"),
    pytest.param(ChatMessage.user("Late input"), False, id="user-message-stale"),
]
_COMPACTION_OUTCOMES = {
    COMPACTION_STARTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
    COMPACTION_ABORTED_EVENT,
}


def _outcomes(events: list[Any]) -> list[str]:
    return [event.type for event in events if event.type in _COMPACTION_OUTCOMES]


def _live_context_notes(messages: list[ChatMessage]) -> list[str]:
    return [
        str(message.content)
        for message in effective_compaction_messages(messages)
        if message.role == "note"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("concurrent", "carried"), _CONCURRENT_ENTRIES)
async def test_final_answer_compaction_meets_a_concurrent_entry(
    tmp_path: Path, concurrent: ChatMessage, carried: bool
) -> None:
    runtime = compaction_runtime(
        tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[]),
        adapter=StubAdapter(
            [
                {
                    "content": "Finished",
                    "tool_calls": None,
                    "usage": {"input_tokens": 90, "output_tokens": 5},
                }
            ]
        ),
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    service = CompactOnceService(block=True)
    address = session_address("coder", "session-one")

    run = await build_chat_loop(runtime, compaction_service=cast(Any, service)).start_run(
        "coder", "Finish", session_id="session-one"
    )
    await append_while_compacting(runtime, service, address, concurrent)
    result = await asyncio.wait_for(run.wait(), WAIT_SECONDS)

    messages = runtime.chat_sessions.get(address).load()
    events = await runtime.timelines.events(run)
    assert result.content == "Finished"
    if carried:
        assert persisted_roles(messages) == ["user", "assistant", "note", "compaction_checkpoint"]
        assert _outcomes(events) == [COMPACTION_STARTED_EVENT, COMPACTION_COMPLETED_EVENT]
        assert _live_context_notes(messages)[-1] == "Background completed"
    else:
        assert persisted_roles(messages) == ["user", "assistant", "user"]
        assert _outcomes(events) == [COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT]
        aborted = next(event for event in events if event.type == COMPACTION_ABORTED_EVENT)
        assert aborted.payload == {"reason": "stale_context"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("concurrent", "carried"), _CONCURRENT_ENTRIES)
async def test_mid_tool_compaction_meets_a_concurrent_entry_and_rebuilds_the_request(
    tmp_path: Path, concurrent: ChatMessage, carried: bool
) -> None:
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"weather": "sunny"}),
    )
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
    runtime = compaction_runtime(
        tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"]),
        adapter=adapter,
        tools=tools,
        context_window=2_000,
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    service = CompactOnceService(block=True)
    address = session_address("coder", "session-one")

    run = await build_chat_loop(runtime, compaction_service=cast(Any, service)).start_run(
        "coder", "Weather?", session_id="session-one"
    )
    await append_while_compacting(runtime, service, address, concurrent)
    result = await asyncio.wait_for(run.wait(), WAIT_SECONDS)

    second_request = "\n".join(
        str(message.get("content", "")) for message in adapter.requests[1]["messages"]
    )
    assert result.content == "Sunny"
    assert persisted_roles(runtime.chat_sessions.get(address).load()) == [
        "user",
        "assistant",
        "tool",
        concurrent.role,
        *(["compaction_checkpoint"] if carried else []),
        "assistant",
    ]
    if concurrent.role == "note":
        assert "<system-reminder>\nBackground completed\n</system-reminder>" in second_request
    else:
        assert "Late input" in second_request
    assert _outcomes(await runtime.timelines.events(run)) == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_COMPLETED_EVENT if carried else COMPACTION_ABORTED_EVENT,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("landing_notes", "committed"),
    [
        pytest.param(AUTO_COMPACTION_COMMIT_ATTEMPTS - 1, True, id="settles-within-the-bound"),
        pytest.param(AUTO_COMPACTION_COMMIT_ATTEMPTS, False, id="outruns-the-bound"),
    ],
)
async def test_automatic_compaction_absorbs_notes_landing_during_projection_up_to_a_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, landing_notes: int, committed: bool
) -> None:
    # Each note lands while one attempt projects the post-Compaction request, so
    # that attempt's commit fails and the next one absorbs the note.
    runtime = compaction_runtime(tmp_path)
    agent = runtime.agents.get("coder")
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    request = await build_request_messages(loop, agent, session)
    build_request_state = loop._requests.build_request_state
    landed: list[str] = []

    async def build_while_a_note_lands(*args: Any, **kwargs: Any) -> _RequestState:
        if len(landed) < landing_notes:
            landed.append(f"Note {len(landed) + 1}")
            async with runtime.chat_sessions.write_lock(session.address):
                await runtime.chat_sessions.get(session.address).add_note_async(landed[-1])
        return cast(_RequestState, await build_request_state(*args, **kwargs))

    monkeypatch.setattr(loop._requests, "build_request_state", build_while_a_note_lands)

    probe = await auto_compact(loop, agent, session, usage={"input_tokens": 90}, request=request)

    messages = session.load()
    assert len(service.compact_calls) == 1
    if committed:
        assert persisted_roles(messages) == [
            "user",
            "assistant",
            *["note"] * landing_notes,
            "compaction_checkpoint",
        ]
        assert _live_context_notes(messages)[-landing_notes:] == landed
        assert _outcomes(probe.run.events) == [COMPACTION_STARTED_EVENT, COMPACTION_COMPLETED_EVENT]
    else:
        assert persisted_roles(messages) == ["user", "assistant", *["note"] * landing_notes]
        assert _outcomes(probe.run.events) == [COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT]


@pytest.mark.asyncio
async def test_stop_during_post_answer_compaction_keeps_the_answer_resolved(
    tmp_path: Path,
) -> None:
    service = CompactOnceService(block=True)
    runtime = compaction_runtime(
        tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[]),
        adapter=StubAdapter(
            [{"content": "Complete answer", "usage": {"input_tokens": 90, "output_tokens": 5}}]
        ),
    )
    runtime.chat_sessions.create("coder", session_id="one")
    run = await build_chat_loop(runtime, compaction_service=cast(Any, service)).start_run(
        "coder", "Finish", session_id="one"
    )
    await asyncio.wait_for(service.started.wait(), WAIT_SECONDS)

    await runtime.chat_run_manager.cancel(run.id, reason="user")

    with pytest.raises(RunCancelledError):
        await run.wait()
    assert run.status == RunStatus.CANCELLED
    session = runtime.chat_sessions.get(session_address("coder", "one"))
    assistant = next(message for message in session.load() if message.role == "assistant")
    assert (assistant.content, assistant.interrupted) == ("Complete answer", False)
    assert session.load_continuation() is None
