"""The chat loop Tool cycle: dispatch, persistence, continuation and cancellation."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatMessage
from core.runs import (
    MODEL_STEP_USAGE_EVENT,
    TOOL_CALL_RESULT_EVENT,
    RunCancelledError,
    RunStatus,
)
from core.sessions import ChatSession
from core.sessions.store import SessionStore
from core.tools import (
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.tools.process_manager import ProcessManager, ProcessManagerError
from tests.core.chat.chat_loop_support import (
    StubModels,
    StubStorage,
    build_chat_loop,
    history,
    persisted_roles,
)
from tests.core.chat.chat_loop_tools_test_support import (
    WAIT_SECONDS,
    JsonObject,
    final,
    tool_results,
    tool_runtime,
    tool_turn,
)


def _weather_tools(**options: Any) -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
        **options,
    )
    return tools


_WEATHER_TURN = tool_turn(
    ("call_abc", "get_weather", {"city": "Berlin"}),
    reasoning="Need weather.",
    reasoning_meta={"encrypted_content": "opaque-current-turn"},
    usage={"input_tokens": 11, "output_tokens": 7},
)


@pytest.mark.asyncio
async def test_send_dispatches_tool_and_resends_context_until_final(tmp_path: Path) -> None:
    runtime = tool_runtime(
        tmp_path,
        _weather_tools(display=ToolDisplay(summary_fields=("city",))),
        [_WEATHER_TURN, final("Sunny")],
    )

    assistant = await build_chat_loop(runtime).send("coder", "Weather?", session_id="session-one")

    persisted = [message.to_dict() for message in history(runtime)]
    adapter = runtime.adapter
    assert assistant.content == "Sunny"
    assert [message["role"] for message in persisted] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "run_summary",
    ]
    assert persisted[1]["reasoning_meta"] == {"encrypted_content": "opaque-current-turn"}
    assert persisted[2]["tool_call_id"] == "call_abc"
    assert persisted[2]["timing"]["duration_ms"] >= 0
    assert json.loads(persisted[2]["content"]) == tool_success({"temp": 22, "city": "Berlin"})
    assert persisted[4]["run_id"]
    assert persisted[4]["status"] == "completed"
    assert persisted[4]["timing"]["duration_ms"] >= 0
    continuation = adapter.requests[1]["messages"]
    assert [message["role"] for message in continuation] == ["system", "user", "assistant", "tool"]
    assert continuation[2]["reasoning_meta"] == {"encrypted_content": "opaque-current-turn"}
    assert continuation[2]["reasoning"] == "Need weather."
    # usage is persisted on the assistant turn but never sent to the provider.
    assert persisted[1]["usage"]["input_tokens"] == 11
    assert persisted[1]["usage"]["output_tokens"] == 7
    assert "usage" not in continuation[2]
    assert "timing" not in continuation[3]
    run = runtime.chat_runs.get(persisted[4]["run_id"])
    events = await runtime.timelines.events(run)
    [tool_result_event] = [event for event in events if event.type == TOOL_CALL_RESULT_EVENT]
    assert tool_result_event.payload["timing"]["duration_ms"] >= 0
    usage_events = [event for event in events if event.type == MODEL_STEP_USAGE_EVENT]
    assistant_turns = [message for message in persisted if message["role"] == "assistant"]
    assert [event.payload["usage"] for event in usage_events] == [
        message["usage"] for message in assistant_turns
    ]
    assert usage_events[0].payload["context_usage"] == {
        "tokens": 45,
        "estimated": True,
        "estimated_delta_tokens": 34,
        "provider_input_tokens": 11,
        "provider_output_tokens": 7,
    }
    measured_session_usage = {
        "measured_turns": 1,
        "estimated_turns": 0,
        "cache_turns": 0,
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert usage_events[0].payload["session_usage"] == measured_session_usage
    assert usage_events[1].payload["usage"]["estimated"] is True
    assert usage_events[1].payload["context_usage"]["estimated"] is True
    assert usage_events[1].payload["context_usage"]["tokens"] > 18
    assert usage_events[1].payload["session_usage"] == {
        **measured_session_usage,
        "estimated_turns": 1,
    }


@pytest.mark.asyncio
async def test_sibling_calls_run_concurrently_and_persist_in_call_order_as_one_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_started = asyncio.Event()
    first_can_finish = asyncio.Event()

    async def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        if context.tool_call_id == "call_1":
            # Completes only once its sibling of the same Tool has started.
            await asyncio.wait_for(second_started.wait(), timeout=WAIT_SECONDS)
            first_can_finish.set()
        else:
            second_started.set()
        return tool_success({"id": context.tool_call_id})

    tools = ToolRegistry()
    tools.register("probe", "Return the probe id.", {"type": "object"}, probe)
    runtime = tool_runtime(
        tmp_path, tools, [tool_turn(("call_1", "probe"), ("call_2", "probe")), final("Done")]
    )
    batches: list[list[str]] = []
    original_append_many_async = ChatSession.append_many_async

    async def recording_append_many(
        self: ChatSession, messages: list[ChatMessage], **options: Any
    ) -> Any:
        batches.append([message.role for message in messages])
        return await original_append_many_async(self, messages, **options)

    monkeypatch.setattr(ChatSession, "append_many_async", recording_append_many)

    assistant = await build_chat_loop(runtime).send("coder", "Run tools", session_id="session-one")

    messages = history(runtime)
    run = runtime.chat_runs.get(messages[-1].run_id)
    events = await runtime.timelines.events(run)
    assert assistant.content == "Done"
    assert first_can_finish.is_set()
    assert [batch for batch in batches if "tool" in batch] == [["tool", "tool"]]
    assert [result["data"]["id"] for result in tool_results(messages)] == ["call_1", "call_2"]
    assert [
        event.payload["tool_call"]["id"] for event in events if event.type == TOOL_CALL_RESULT_EVENT
    ] == ["call_2", "call_1"]


@pytest.mark.asyncio
async def test_tool_cycle_boundaries_need_no_separate_journal_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = 0
    writes_at: dict[str, int] = {}

    def probe(context: Any, _arguments: Any) -> Any:
        writes_at["handler"] = writes
        return tool_success({"id": context.tool_call_id})

    tools = ToolRegistry()
    tools.register("probe", "Return the probe id.", {"type": "object"}, probe)
    runtime = tool_runtime(tmp_path, tools, [tool_turn(("first", "probe")), final("done")])
    send = runtime.adapter.send

    async def observing_send(messages: Any, *, model_id: str, **kwargs: Any) -> Any:
        writes_at.setdefault("first_request", writes)
        return await send(messages, model_id=model_id, **kwargs)

    monkeypatch.setattr(runtime.adapter, "send", observing_send)
    runtime.chat_sessions.create("coder", session_id="session-one")
    journal_writes: list[list[str]] = []
    original_append_continuation = SessionStore.append_continuation

    def recording_append_continuation(
        self: SessionStore, address: Any, records: list[JsonObject]
    ) -> None:
        journal_writes.append([str(record["type"]) for record in records])
        original_append_continuation(self, address, records)

    monkeypatch.setattr(SessionStore, "append_continuation", recording_append_continuation)
    store = runtime.chat_sessions._store
    execute_write = store._execute_write

    def counting_write(*args: Any, **kwargs: Any) -> Any:
        nonlocal writes
        writes += 1
        return execute_write(*args, **kwargs)

    monkeypatch.setattr(store, "_execute_write", counting_write)

    await build_chat_loop(runtime, streaming=False).send(
        "coder", "probe once", session_id="session-one"
    )

    # The journal starts with the input append, and Assistant boundaries and
    # Tool Results commit inside their history writes.
    assert journal_writes == []
    # Only the Assistant append separates the Model response from the Tool
    # handler: starting a Tool writes nothing.
    assert writes_at["handler"] == writes_at["first_request"] + 1
    assert persisted_roles(history(runtime))[-3:] == ["assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_tool_result_persistence_callback_observes_durable_result(tmp_path: Path) -> None:
    observed_roles: list[list[str]] = []
    runtime_holder: list[Any] = []

    def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        context.after_result_persisted(
            lambda: observed_roles.append(persisted_roles(history(runtime_holder[0])))
        )
        return tool_success({"value": "ready"})

    tools = ToolRegistry()
    tools.register("probe", "Probe persistence.", {"type": "object"}, probe)
    runtime = tool_runtime(tmp_path, tools, [tool_turn(("call_probe", "probe")), final("done")])
    runtime_holder.append(runtime)

    await build_chat_loop(runtime).send("coder", "Run probe", session_id="session-one")

    assert observed_roles == [["user", "assistant", "tool"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["start_run", "queue_run"])
async def test_internal_input_persistence_callback_observes_the_durable_note(
    tmp_path: Path, entry: str
) -> None:
    runtime = tool_runtime(tmp_path, None, [final("handled")], allowed_tools=[])
    observed_roles: list[list[str]] = []
    loop = build_chat_loop(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")

    started = await getattr(loop, entry)(
        "coder",
        "background result",
        session_id="session-one",
        internal=True,
        input_persisted_hook=lambda: observed_roles.append(persisted_roles(history(runtime))),
    )
    run = await started.future if entry == "queue_run" else started
    await run.wait()

    assert observed_roles == [["note"]]


@pytest.mark.asyncio
async def test_auto_compaction_preserves_active_tool_continuation_reasoning(
    tmp_path: Path,
) -> None:
    class SingleCheckpointCompactionService:
        def __init__(self) -> None:
            self.compacted = False
            self.compact_calls = 0
            self.request_messages: list[JsonObject] = []
            self.checks = 0

        def has_new_compactable_context(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

        def should_auto_compact(self, *_args: Any, **_kwargs: Any) -> bool:
            self.checks += 1
            return self.checks == 2 and not self.compacted

        async def compact(self, messages: list[ChatMessage], **kwargs: Any) -> ChatMessage:
            self.compacted = True
            self.compact_calls += 1
            self.request_messages = [dict(message) for message in kwargs["request_messages"]]
            tail_user = next(
                message
                for message in messages
                if message.role == "user" and message.content == "Weather?"
            )
            return ChatMessage.compaction_checkpoint(
                summary="Compacted prior context.",
                projection=messages[messages.index(tail_user) :],
                compacted_token_count=42,
            )

    runtime = tool_runtime(
        tmp_path,
        _weather_tools(),
        [_WEATHER_TURN, final("Sunny")],
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    compaction_service = SingleCheckpointCompactionService()

    assistant = await build_chat_loop(
        runtime, compaction_service=cast(Any, compaction_service)
    ).send("coder", "Weather?", session_id="session-one")

    requests = runtime.adapter.requests
    continued_messages = requests[1]["messages"]
    assert assistant.content == "Sunny"
    assert compaction_service.compact_calls == 1
    assert compaction_service.request_messages[:2] == requests[0]["messages"]
    assert [message["role"] for message in compaction_service.request_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert compaction_service.request_messages[2]["reasoning"] == "Need weather."
    assert compaction_service.request_messages[3]["tool_call_id"] == "call_abc"
    # Compaction between two requests of one Run leaves the offered Tool list unchanged.
    assert requests[1]["kwargs"]["tools"] == requests[0]["kwargs"]["tools"]
    assert [message["role"] for message in continued_messages] == [
        "system",
        "user",
        "user",
        "assistant",
        "tool",
    ]
    reminder = continued_messages[1]["content"]
    assert reminder.startswith("<system-reminder>\n")
    assert reminder.endswith("\n</system-reminder>")
    assert "Compacted prior context." in reminder
    assert continued_messages[3]["reasoning"] == "Need weather."
    assert continued_messages[3]["reasoning_meta"] == {"encrypted_content": "opaque-current-turn"}
    assert "usage" not in continued_messages[3]


@pytest.mark.asyncio
async def test_real_run_cancel_during_parallel_tools_repairs_the_next_request(
    tmp_path: Path,
) -> None:
    slow_started = asyncio.Event()
    slow_release = asyncio.Event()
    cancel_callbacks: list[str] = []

    async def fast_probe(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        await slow_started.wait()
        return tool_success({"probe": "fast"})

    async def slow_probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        def cancel() -> None:
            cancel_callbacks.append(context.tool_call_id)
            slow_release.set()

        context.on_cancel(cancel)
        slow_started.set()
        await slow_release.wait()
        return tool_failure("cancelled_by_user", "Slow probe cancelled.")

    tools = ToolRegistry()
    tools.register("fast_probe", "Complete first.", {"type": "object"}, fast_probe)
    tools.register("slow_probe", "Remain active until cancelled.", {"type": "object"}, slow_probe)
    runtime = tool_runtime(
        tmp_path,
        tools,
        [
            tool_turn(("call_fast", "fast_probe"), ("call_slow", "slow_probe")),
            final("Recovered on the next Run."),
        ],
    )
    loop = build_chat_loop(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")
    cancelled_run = await loop.start_run("coder", "Run both probes.", session_id="session-one")

    async def fast_result_emitted() -> None:
        while not any(
            event.type == TOOL_CALL_RESULT_EVENT and event.payload["tool_call"]["id"] == "call_fast"
            for event in cancelled_run.events
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(fast_result_emitted(), timeout=WAIT_SECONDS)
    cancelled_run.request_cancel(reason="user")
    with pytest.raises(RunCancelledError):
        await cancelled_run.wait()

    after_cancel = history(runtime)
    assert cancel_callbacks == ["call_slow"]
    # The settled Run releases its process scope only after cancelling it.
    assert runtime.process_manager.scope_events == [
        ("cancel", cancelled_run.id),
        ("release", cancelled_run.id),
    ]
    assert persisted_roles(after_cancel) == ["user", "assistant"]
    assert [m.status for m in after_cancel if m.role == "run_summary"] == ["cancelled"]

    recovered = await loop.send("coder", "Continue safely.", session_id="session-one")

    assert recovered.content == "Recovered on the next Run."
    repaired_results = [
        message
        for message in runtime.adapter.requests[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in repaired_results] == ["call_fast", "call_slow"]
    for message in repaired_results:
        assert json.loads(message["content"])["error"]["code"] == "result_unavailable"
    final_history = history(runtime)
    assert persisted_roles(final_history) == ["user", "assistant", "user", "assistant"]
    assert [m.status for m in final_history if m.role == "run_summary"] == [
        "cancelled",
        "completed",
    ]


@pytest.mark.asyncio
async def test_cooperative_stop_after_a_tool_batch_persists_every_sibling_first(
    tmp_path: Path,
) -> None:
    # The cancel flag is raised once every parallel sibling has started, without the
    # forceful task cancel above, so the loop honors it only at its boundary after the
    # batch persistence and never leaves a dangling Tool Call turn behind.
    started: set[str] = set()
    all_started = asyncio.Event()
    release = asyncio.Event()

    async def sibling(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        started.add(context.tool_call_id)
        if len(started) == 3:
            all_started.set()
        await release.wait()
        return tool_success({"sibling": context.tool_call_id})

    tools = ToolRegistry()
    for name in ("first", "second", "third"):
        tools.register(name, "Parallel sibling.", {"type": "object"}, sibling, parallel_safe=True)
    runtime = tool_runtime(
        tmp_path,
        tools,
        [tool_turn(("call_first", "first"), ("call_second", "second"), ("call_third", "third"))],
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    run = await build_chat_loop(runtime).start_run("coder", "Multi", session_id="session-one")

    await asyncio.wait_for(all_started.wait(), timeout=WAIT_SECONDS)
    run.cancel_requested = True
    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()

    persisted = history(runtime)
    assert [m.tool_call_id for m in persisted if m.role == "tool"] == [
        "call_first",
        "call_second",
        "call_third",
    ]
    assert (persisted[-1].role, persisted[-1].status) == ("run_summary", "cancelled")
    activity = runtime.chat_sessions.list_summaries("coder")[0]
    assert (activity["run_kinds"], activity["unread_run_id"], activity["unread_run_status"]) == (
        ["user"],
        run.id,
        "cancelled",
    )


@pytest.mark.asyncio
async def test_run_cancel_rejects_a_racing_launch_and_releases_the_settled_scope(
    tmp_path: Path,
) -> None:
    process_manager = ProcessManager(sweep_interval_seconds=3600)
    tool_started = asyncio.Event()
    launch_outcomes: list[str] = []

    async def late_launch(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        tool_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A launch arriving after the Run cancel but before the Run settles;
            # the Process manager must refuse it, so no child process starts.
            try:
                await process_manager.spawn(
                    context.run_id,
                    context.agent_id,
                    [sys.executable, "-c", "pass"],
                    env=None,
                    cwd=None,
                )
            except ProcessManagerError:
                launch_outcomes.append("rejected")
            else:
                launch_outcomes.append("started")
            raise
        return tool_success({})

    tools = ToolRegistry()
    tools.register("late_launch", "Launch after cancellation.", {"type": "object"}, late_launch)
    runtime = tool_runtime(tmp_path, tools, [tool_turn(("call_late", "late_launch"))])
    runtime.process_manager = process_manager
    loop = build_chat_loop(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")
    try:
        run = await loop.start_run("coder", "Launch late.", session_id="session-one")
        await asyncio.wait_for(tool_started.wait(), WAIT_SECONDS)

        run.request_cancel(reason="user")
        with pytest.raises(RunCancelledError):
            await run.wait()

        assert launch_outcomes == ["rejected"]
        assert process_manager.list_processes("coder") == []
        # The settled Run no longer retains its closed-scope marker.
        assert process_manager._closed_scopes == set()
    finally:
        await process_manager.aclose()


@pytest.mark.asyncio
async def test_per_call_cancel_persists_cancelled_and_completed_siblings_in_order(
    tmp_path: Path,
) -> None:
    cancellable_started = asyncio.Event()
    cancel_fired = asyncio.Event()
    sibling_started = asyncio.Event()
    sibling_release = asyncio.Event()

    async def cancellable(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        context.on_cancel(cancel_fired.set)
        cancellable_started.set()
        await cancel_fired.wait()
        assert context.was_cancelled_by_user() is True
        return tool_failure("cancelled_by_user", "Cancelled by the user.")

    async def sibling(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        sibling_started.set()
        await sibling_release.wait()
        return tool_success({"sibling": "completed"})

    tools = ToolRegistry()
    tools.register("cancellable", "Wait for cancellation.", {"type": "object"}, cancellable)
    tools.register("sibling", "Complete beside a cancelled Tool.", {"type": "object"}, sibling)
    runtime = tool_runtime(
        tmp_path,
        tools,
        [
            tool_turn(("call_cancel", "cancellable"), ("call_sibling", "sibling")),
            final("Both Results observed."),
        ],
    )
    loop = build_chat_loop(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await loop.start_run("coder", "Run siblings.", session_id="session-one")
    await asyncio.wait_for(
        asyncio.gather(cancellable_started.wait(), sibling_started.wait()), timeout=WAIT_SECONDS
    )
    assert run.cancel_tool_call("call_cancel") is True
    sibling_release.set()
    assistant = await run.wait()

    assert assistant.content == "Both Results observed."
    assert (run.status, run.cancel_requested) == (RunStatus.COMPLETED, False)
    persisted_tools = [message for message in history(runtime) if message.role == "tool"]
    assert [message.tool_call_id for message in persisted_tools] == ["call_cancel", "call_sibling"]
    cancelled, completed = tool_results(persisted_tools)
    assert cancelled["error"]["code"] == "cancelled_by_user"
    assert completed == tool_success({"sibling": "completed"})
