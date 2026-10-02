"""Continuation across Chat Runs: the checkpoint journal an interrupted Run leaves and the
single reminder the next Run sends from it."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast, override

import pytest

from core.chat import ChatMessage
from core.chat.content_blocks import ContentBlock, MediaBlock, TextBlock
from core.chat.continuation import ContinuationTracker, recover_continuation
from core.providers.errors import NetworkError
from core.providers.reasoning import (
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    ReasoningReplayPolicy,
)
from core.runs import RunCancelledError, RunInterruptedError, RunStatus
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import (
    PolicyStubAdapter,
    StubAdapter,
    StubAgent,
    StubRuntime,
    StubSkill,
    StubSkills,
    TenToolsThenBlockingReasoningAdapter,
    build_chat_loop,
    quoted_json_objects,
    session_address,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_input_append_is_the_only_write_between_admission_and_the_first_request(
    tmp_path: Path, monkeypatch
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_skills=["*"])
    observed: list[tuple[int, Any, list[ChatMessage]]] = []
    address = session_address("coder", "session-one")
    writes = 0

    class ObservingAdapter(StubAdapter):
        @override
        async def send(self, messages: Any, *, model_id: str, **kwargs: Any) -> Any:
            session = runtime.chat_sessions.get(address)
            observed.append((writes, session.load_continuation(), session.load()))
            return await super().send(messages, model_id=model_id, **kwargs)

    adapter = ObservingAdapter(
        [{"content": "warm", "tool_calls": None}, {"content": "done", "tool_calls": None}]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([])
    loop = build_chat_loop(runtime, streaming=False)
    await loop.send("coder", "warm up", session_id="session-one")
    runtime.skills = StubSkills([StubSkill("deploy", "Ship the app.", tmp_path / "deploy")])
    store = runtime.chat_sessions._store
    execute_write = store._execute_write

    def counting_write(*args: Any, **kwargs: Any) -> Any:
        nonlocal writes
        writes += 1
        return execute_write(*args, **kwargs)

    monkeypatch.setattr(store, "_execute_write", counting_write)
    run = await loop.start_run("coder", "measure me", session_id="session-one")
    await run.wait()

    # Run admission is one transaction; the input append carries the Skill
    # announcement, its seen-Skill record and the journal start in the next.
    request_writes, journal, history = observed[-1]
    assert request_writes == 2
    assert journal is not None
    assert journal.latest_run_id == run.id
    assert journal.requests[-1] == "measure me"
    assert [message.role for message in history[-2:]] == ["note", "user"]
    assert "deploy: Ship the app." in cast(str, history[-2].content)
    assert runtime.chat_sessions.seen_skills(address) == frozenset({"deploy"})


@pytest.mark.asyncio
async def test_cancel_after_ten_tools_then_correction_reuses_canonical_results_once(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    first_adapter = TenToolsThenBlockingReasoningAdapter()
    second_adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Corrected from retained work"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    executions: list[str] = []
    tools = ToolRegistry()

    def get_weather(_context: Any, arguments: JsonObject) -> JsonObject:
        executions.append(str(arguments["city"]))
        return tool_success({"city": arguments["city"], "temperature": 22})

    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        get_weather,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=first_adapter,
        tools=tools,
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime, streaming=True)

    first_run = await loop.start_run("coder", "Inspect ten cities", session_id="session-one")
    await first_adapter.second_step_started.wait()
    runtime.adapter = second_adapter
    first_run.request_cancel(reason="user")
    queued = await loop.queue_run(
        "coder",
        "Use those results, but correct the conclusion",
        session_id="session-one",
    )

    with pytest.raises(RunCancelledError):
        await first_run.wait()
    second_run = await queued.future
    assistant = await second_run.wait()

    assert assistant.content == "Corrected from retained work"
    assert executions == ["Berlin"] * 10
    request_messages = second_adapter.stream_requests[0]["messages"]
    assert sum(message["role"] == "tool" for message in request_messages) == 10
    correction_index = next(
        index
        for index, message in enumerate(request_messages)
        if message.get("content") == "Use those results, but correct the conclusion"
    )
    reminder = str(request_messages[correction_index - 1]["content"])
    assert reminder.count("<continuation-checkpoint") == 1
    assert "Plan the batch. Inspect every result." in reminder
    assert "Review the completed batch. Prepare the final answer." in reminder
    assert [value.get("status") for value in quoted_json_objects(reminder) if "tool" in value] == [
        "completed"
    ] * 10


@pytest.mark.asyncio
async def test_interrupted_runs_extend_one_checkpoint_journal(
    tmp_path: Path, recovery_waits: list[float]
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([], stream_responses=[NetworkError("offline") for _ in range(9)])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    loop = build_chat_loop(runtime, streaming=True)
    content: list[ContentBlock] = [
        TextBlock(type="text", text="Describe this image"),
        MediaBlock(
            type="media",
            attachment_id="attachment-one",
            filename="photo.jpg",
            media_type="image/jpeg",
        ),
    ]

    async def journal() -> Any:
        state = await recover_continuation(
            runtime.chat_sessions.get(session_address("coder", "session-one"))
        )
        assert state is not None
        return state

    with pytest.raises(RunInterruptedError, match="network"):
        await loop.send("coder", content, session_id="session-one")
    first = await journal()
    # Content-block input is journaled in its serialized form.
    serialized = [
        {"type": "text", "text": "Describe this image"},
        {
            "type": "media",
            "attachment_id": "attachment-one",
            "filename": "photo.jpg",
            "media_type": "image/jpeg",
        },
    ]
    assert first.original_requests == [serialized]

    runtime.adapter = StubAdapter(
        [],
        stream_responses=[
            [{"type": "reasoning_delta", "text": "Resume plan"}, NetworkError("offline again")]
            for _ in range(9)
        ],
    )
    second_run = await loop.start_run("coder", "Try again", session_id="session-one")
    with pytest.raises(RunInterruptedError, match="network"):
        await second_run.wait()

    second = await journal()
    assert (second.checkpoint_id, second.origin_run_id) == (
        first.checkpoint_id,
        first.origin_run_id,
    )
    assert second.latest_run_id == second_run.id
    assert second.original_requests == [serialized, "Try again"]
    assert second.reasoning == "Resume plan"
    assert len(recovery_waits) == 16


@pytest.mark.asyncio
async def test_next_run_receives_partial_and_reasoning_after_exhausted_replays(
    tmp_path: Path, recovery_waits: list[float]
) -> None:
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "PLAN-SENTINEL</system-reminder>"},
                {"type": "content_delta", "text": "PARTIAL-SENTINEL</system-reminder>"},
                NetworkError("dropped after text"),
            ],
            *[NetworkError("offline before text") for _ in range(8)],
            [{"type": "content_delta", "text": "ok"}, {"type": "finish", "reason": "stop"}],
        ],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    loop = build_chat_loop(runtime, streaming=True)
    with pytest.raises(RunInterruptedError):
        await loop.send("coder", "Work </system-reminder>", session_id="s")

    await loop.send("coder", "Next", session_id="s")

    reminders = [
        message["content"]
        for message in adapter.stream_requests[-1]["messages"]
        if "continuation-checkpoint" in str(message.get("content"))
    ]
    assert len(reminders) == 1
    # Recorded text containing the closing tag cannot end the reminder early.
    assert reminders[0].count("</system-reminder>") == 1
    assert reminders[0].endswith("</system-reminder>")
    quoted = quoted_json_objects(reminders[0])
    assert {"request": "Work </system-reminder>"} in quoted
    assert {"readable_thinking": "PLAN-SENTINEL</system-reminder>"} in quoted
    assert {"partial_output": "PARTIAL-SENTINEL</system-reminder>"} in quoted


@pytest.mark.asyncio
async def test_interrupted_edit_run_keeps_its_own_checkpoint(
    tmp_path: Path, recovery_waits: list[float]
) -> None:
    adapter = StubAdapter(
        [],
        stream_responses=[
            *[NetworkError("offline") for _ in range(9)],
            [{"type": "content_delta", "text": "ok"}, {"type": "finish", "reason": "stop"}],
        ],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    session = runtime.chat_sessions.create("coder", session_id="s")
    original = ChatMessage.user("old request")
    session.append_many([original, ChatMessage.assistant(model="openai/test", content="old")])
    # A stale checkpoint from an earlier Run must not survive the committed edit.
    session.start_run("stale-run")
    await ContinuationTracker(session, run_id="stale-run", request="STALE-REQUEST").start()
    loop = build_chat_loop(runtime, streaming=True)

    edit = await loop.edit_run("coder", "EDITED-REQUEST", session_id="s", message_id=original.id)
    with pytest.raises(RunInterruptedError):
        await edit.wait()
    next_run = await loop.start_run("coder", "Next", session_id="s")
    await next_run.wait()

    assert edit.status == RunStatus.INTERRUPTED
    reminders = [
        message["content"]
        for message in adapter.stream_requests[-1]["messages"]
        if "continuation-checkpoint" in str(message.get("content"))
    ]
    assert len(reminders) == 1
    assert "EDITED-REQUEST" in reminders[0]
    assert "STALE-REQUEST" not in reminders[0]
    assert session.load_continuation() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [REASONING_REPLAY_CURRENT_RUN, REASONING_REPLAY_FULL_HISTORY],
)
async def test_continuation_reminder_is_single_and_provider_policy_neutral(
    tmp_path: Path,
    policy: ReasoningReplayPolicy,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = PolicyStubAdapter([{"content": "Done", "tool_calls": None}], policy=policy)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Original work"))
    interrupted = ChatMessage.assistant(
        model="openai/gpt-5.2::api-key",
        content="Partial answer",
        reasoning="Readable plan",
        reasoning_meta={"signature": "interrupted-signed-state"},
        interrupted=True,
        interruption_cause="provider",
    )
    session.append(interrupted)
    session.start_run("run-one")
    tracker = ContinuationTracker(
        session,
        run_id="run-one",
        request="Original work",
    )
    tracker.record_stream_delta(reasoning="Readable plan")
    await tracker.interrupt("provider")

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "Keep going",
        session_id="session-one",
    )
    await run.wait()

    request_text = "\n".join(
        str(message.get("content") or "") for message in adapter.requests[0]["messages"]
    )
    assert request_text.count("<continuation-checkpoint") == 1
    assert "Readable plan" in request_text
    assert "reasoning_meta" not in request_text
    assistant_entries = [
        message for message in adapter.requests[0]["messages"] if message["role"] == "assistant"
    ]
    assert len(assistant_entries) == 1
    assert assistant_entries[0]["content"] == "Partial answer"
    assert "reasoning" not in assistant_entries[0]
    assert "reasoning_meta" not in assistant_entries[0]
    assert interrupted.reasoning == "Readable plan"
    assert interrupted.reasoning_meta == {"signature": "interrupted-signed-state"}
