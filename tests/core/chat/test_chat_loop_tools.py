"""Chat-loop tests grouped by tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ChatError,
    ChatMessage,
)
from core.chat.chat import (
    MAX_IDENTICAL_FAILED_TOOL_CALLS,
    _FailedToolCallCircuitBreaker,
)
from core.chat.messages import HISTORY_COMPACTION_GUIDANCE, ToolCall
from core.model_tasks import TASK_IMAGE_UNDERSTANDING
from core.runs import (
    MODEL_STEP_USAGE_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    RunCancelledError,
    RunStatus,
)
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    HISTORY_TOOL_NAME,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    register_glob_tool,
    register_grep_tool,
    register_history_tool,
    tool_failure,
    tool_success,
)
from core.tools import JsonObject as ToolJsonObject
from core.utils.errors import ProviderError
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubModels,
    StubRuntime,
    StubStorage,
    build_chat_loop,
    persisted_roles,
)

JsonObject = dict[str, Any]


def _analyze_image_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ANALYZE_IMAGE_TOOL_NAME,
        "Analyze images.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"analysis": "ok"}),
    )
    return registry


def _request_tool_names(adapter: StubAdapter) -> set[str]:
    tools = adapter.requests[0]["kwargs"]["tools"]
    return {str(definition["name"]) for definition in tools}


@pytest.mark.asyncio
async def test_analyze_image_visible_for_nonvision_route_with_usable_binding(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/text-model",
        allowed_tools=[ANALYZE_IMAGE_TOOL_NAME],
    )
    adapter = StubAdapter([{"content": "done", "tool_calls": None}])
    models = StubModels(
        {("openai", "text-model"): 128_000},
        input_modalities={("openai", "text-model"): ("text",)},
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=_analyze_image_registry(),
        models=models,
        available_task_models={TASK_IMAGE_UNDERSTANDING},
    )

    await build_chat_loop(runtime).send("coder", "Inspect the image", session_id="s1")

    assert ANALYZE_IMAGE_TOOL_NAME in _request_tool_names(adapter)
    assert ANALYZE_IMAGE_TOOL_NAME in runtime.system_prompts.effective_tool_name_calls[-1]


@pytest.mark.asyncio
async def test_analyze_image_hidden_when_effective_route_can_view_images(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/vision-model",
        allowed_tools=[ANALYZE_IMAGE_TOOL_NAME],
    )
    adapter = StubAdapter(
        [{"content": "done", "tool_calls": None}],
        wire_media_types=frozenset({"image/png"}),
    )
    models = StubModels(
        {("openai", "vision-model"): 128_000},
        input_modalities={("openai", "vision-model"): ("text", "image")},
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=_analyze_image_registry(),
        models=models,
        available_task_models={TASK_IMAGE_UNDERSTANDING},
    )

    await build_chat_loop(runtime).send("coder", "Inspect the image", session_id="s1")

    assert ANALYZE_IMAGE_TOOL_NAME not in _request_tool_names(adapter)
    assert ANALYZE_IMAGE_TOOL_NAME not in runtime.system_prompts.effective_tool_name_calls[-1]


@pytest.mark.asyncio
async def test_analyze_image_visible_when_model_has_image_but_wire_does_not(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/vision-model",
        allowed_tools=[ANALYZE_IMAGE_TOOL_NAME],
    )
    adapter = StubAdapter([{"content": "done", "tool_calls": None}])
    models = StubModels(
        {("openai", "vision-model"): 128_000},
        input_modalities={("openai", "vision-model"): ("text", "image")},
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=_analyze_image_registry(),
        models=models,
        available_task_models={TASK_IMAGE_UNDERSTANDING},
    )

    await build_chat_loop(runtime).send("coder", "Inspect the image", session_id="s1")

    assert ANALYZE_IMAGE_TOOL_NAME in _request_tool_names(adapter)


@pytest.mark.asyncio
async def test_analyze_image_hidden_without_usable_binding(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/text-model",
        allowed_tools=[ANALYZE_IMAGE_TOOL_NAME],
    )
    adapter = StubAdapter([{"content": "done", "tool_calls": None}])
    models = StubModels(
        {("openai", "text-model"): 128_000},
        input_modalities={("openai", "text-model"): ("text",)},
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=_analyze_image_registry(),
        models=models,
    )

    await build_chat_loop(runtime).send("coder", "Inspect the image", session_id="s1")

    assert ANALYZE_IMAGE_TOOL_NAME not in _request_tool_names(adapter)


@pytest.mark.asyncio
async def test_send_dispatches_tool_and_resends_context_until_final(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "reasoning": "Need weather.",
                "reasoning_meta": {"encrypted_content": "opaque-current-turn"},
                "usage": {"input_tokens": 11, "output_tokens": 7},
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Sunny", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
        display=ToolDisplay(summary_fields=("city",)),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send("coder", "Weather?", session_id="session-one")

    persisted = [
        message.to_dict() for message in runtime.chat_sessions.get("coder", "session-one").load()
    ]
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
    assert [message["role"] for message in adapter.requests[1]["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert adapter.requests[1]["messages"][2]["reasoning_meta"] == {
        "encrypted_content": "opaque-current-turn"
    }
    assert adapter.requests[1]["messages"][2]["reasoning"] == "Need weather."
    # usage is persisted on the assistant turn but never sent to the provider.
    assert persisted[1]["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert "usage" not in adapter.requests[1]["messages"][2]
    assert "timing" not in adapter.requests[1]["messages"][3]
    tool_result_events = [
        event
        for event in runtime.chat_runs.get(persisted[4]["run_id"]).events
        if event.type == "tool_call_result"
    ]
    assert tool_result_events[0].payload["timing"]["duration_ms"] >= 0
    usage_events = [
        event
        for event in runtime.chat_runs.get(persisted[4]["run_id"]).events
        if event.type == MODEL_STEP_USAGE_EVENT
    ]
    assistant_turns = [message for message in persisted if message["role"] == "assistant"]
    assert [event.payload["usage"] for event in usage_events] == [
        message["usage"] for message in assistant_turns
    ]
    assert usage_events[0].payload["session_usage"] == {
        "measured_turns": 1,
        "estimated_turns": 0,
        "cache_turns": 0,
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert usage_events[1].payload["usage"]["estimated"] is True
    assert usage_events[1].payload["session_usage"] == {
        "measured_turns": 1,
        "estimated_turns": 1,
        "cache_turns": 0,
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


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
    tools.register(
        "fast_probe",
        "Complete while a sibling remains active.",
        {"type": "object"},
        fast_probe,
        parallel_safe=True,
    )
    tools.register(
        "slow_probe",
        "Remain active until cancelled.",
        {"type": "object"},
        slow_probe,
        parallel_safe=True,
    )
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["fast_probe", "slow_probe"],
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_fast", "name": "fast_probe", "arguments": {}},
                    {"id": "call_slow", "name": "slow_probe", "arguments": {}},
                ],
            },
            {"content": "Recovered on the next Run.", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    loop = build_chat_loop(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")

    cancelled_run = await loop.start_run(
        "coder",
        "Run both probes.",
        session_id="session-one",
    )
    await _wait_for_tool_result_event(cancelled_run, "call_fast")

    cancelled_run.request_cancel(reason="user")

    with pytest.raises(RunCancelledError):
        await cancelled_run.wait()

    session = runtime.chat_sessions.get("coder", "session-one")
    after_cancel = session.load()
    assert cancel_callbacks == ["call_slow"]
    assert runtime.process_manager.cancelled_scopes == [cancelled_run.id]
    assert persisted_roles(after_cancel) == ["user", "assistant"]
    assert [message.status for message in after_cancel if message.role == "run_summary"] == [
        "cancelled"
    ]
    assert not any(message.role == "tool" for message in after_cancel)
    assert any(
        event.type == TOOL_CALL_RESULT_EVENT and event.payload["tool_call"]["id"] == "call_fast"
        for event in cancelled_run.events
    )

    recovered = await loop.send(
        "coder",
        "Continue safely.",
        session_id="session-one",
    )

    assert recovered.content == "Recovered on the next Run."
    repaired_request = adapter.requests[1]["messages"]
    repaired_results = [
        message
        for message in repaired_request
        if message.get("role") == "tool"
        and message.get("tool_call_id") in {"call_fast", "call_slow"}
    ]
    assert [message["tool_call_id"] for message in repaired_results] == [
        "call_fast",
        "call_slow",
    ]
    for message in repaired_results:
        result = json.loads(message["content"])
        assert result["error"]["code"] == "result_unavailable"
    final_history = session.load()
    assert not any(message.role == "tool" for message in final_history)
    assert persisted_roles(final_history) == ["user", "assistant", "user", "assistant"]
    assert [message.status for message in final_history if message.role == "run_summary"] == [
        "cancelled",
        "completed",
    ]


@pytest.mark.asyncio
async def test_per_call_cancel_persists_cancelled_and_completed_siblings_in_order(
    tmp_path: Path,
) -> None:
    cancellable_started = asyncio.Event()
    cancel_fired = asyncio.Event()
    sibling_started = asyncio.Event()
    sibling_release = asyncio.Event()
    cancel_callbacks: list[str] = []

    async def cancellable(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        def cancel() -> None:
            cancel_callbacks.append(context.tool_call_id)
            cancel_fired.set()

        context.on_cancel(cancel)
        cancellable_started.set()
        await cancel_fired.wait()
        assert context.was_cancelled_by_user() is True
        return tool_failure("cancelled_by_user", "Cancelled by the user.")

    async def sibling(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        sibling_started.set()
        await sibling_release.wait()
        return tool_success({"sibling": "completed"})

    tools = ToolRegistry()
    tools.register(
        "cancellable",
        "Wait for per-call cancellation.",
        {"type": "object"},
        cancellable,
        parallel_safe=True,
    )
    tools.register(
        "sibling",
        "Complete beside a cancelled Tool.",
        {"type": "object"},
        sibling,
        parallel_safe=True,
    )
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["cancellable", "sibling"],
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_cancel", "name": "cancellable", "arguments": {}},
                    {"id": "call_sibling", "name": "sibling", "arguments": {}},
                ],
            },
            {"content": "Both Results observed.", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    loop = build_chat_loop(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await loop.start_run("coder", "Run siblings.", session_id="session-one")
    await asyncio.wait_for(
        asyncio.gather(cancellable_started.wait(), sibling_started.wait()),
        timeout=5,
    )

    assert run.cancel_tool_call("call_cancel") is True
    sibling_release.set()
    assistant = await run.wait()

    assert assistant.content == "Both Results observed."
    assert run.status is RunStatus.COMPLETED
    assert run.cancel_requested is False
    assert cancel_callbacks == ["call_cancel"]
    persisted_tools = [
        message
        for message in runtime.chat_sessions.get("coder", "session-one").load()
        if message.role == "tool"
    ]
    assert [message.tool_call_id for message in persisted_tools] == [
        "call_cancel",
        "call_sibling",
    ]
    assert json.loads(cast(str, persisted_tools[0].content))["error"]["code"] == (
        "cancelled_by_user"
    )
    assert json.loads(cast(str, persisted_tools[1].content)) == tool_success(
        {"sibling": "completed"}
    )


async def _wait_for_tool_result_event(run: Any, tool_call_id: str) -> None:
    async def result_was_emitted() -> None:
        while not any(
            event.type == TOOL_CALL_RESULT_EVENT
            and event.payload["tool_call"]["id"] == tool_call_id
            for event in run.events
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(result_was_emitted(), timeout=5)


@pytest.mark.asyncio
async def test_tool_result_persistence_callback_observes_durable_result(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["probe"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_probe", "name": "probe", "arguments": {}}],
            },
            {"content": "done", "tool_calls": None},
        ]
    )
    runtime_holder: dict[str, Any] = {}
    observed_roles: list[list[str]] = []

    def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        context.after_result_persisted(
            lambda: observed_roles.append(
                persisted_roles(
                    runtime_holder["runtime"].chat_sessions.get("coder", "session-one").load()
                )
            )
        )
        return tool_success({"value": "ready"})

    tools = ToolRegistry()
    tools.register("probe", "Probe persistence.", {"type": "object"}, probe)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime_holder["runtime"] = runtime

    await build_chat_loop(runtime).send("coder", "Run probe", session_id="session-one")

    assert observed_roles == [["user", "assistant", "tool"]]


@pytest.mark.asyncio
async def test_internal_input_persistence_callback_observes_durable_note(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2")
    adapter = StubAdapter([{"content": "handled", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    observed_roles: list[list[str]] = []
    loop = build_chat_loop(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await loop.start_run(
        "coder",
        "background result",
        session_id="session-one",
        internal=True,
        input_persisted_hook=lambda: observed_roles.append(
            persisted_roles(runtime.chat_sessions.get("coder", "session-one").load())
        ),
    )
    await run.wait()

    assert observed_roles == [["note"]]


@pytest.mark.asyncio
async def test_queued_input_persistence_callback_waits_for_durable_note(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2")
    adapter = StubAdapter([{"content": "handled", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    observed_roles: list[list[str]] = []
    loop = build_chat_loop(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")

    queued = await loop.queue_run(
        "coder",
        "background result",
        session_id="session-one",
        internal=True,
        input_persisted_hook=lambda: observed_roles.append(
            persisted_roles(runtime.chat_sessions.get("coder", "session-one").load())
        ),
    )
    run = await queued.future
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

        def estimate_messages_tokens(self, _messages: list[JsonObject]) -> int:
            return 90

        def should_auto_compact(
            self,
            _input_tokens: int,
            _context_window: int,
            _threshold: float,
        ) -> bool:
            return not self.compacted

        async def compact(
            self,
            messages: list[ChatMessage],
            *,
            agent: Any,
            summary_adapter: Any,
            summary_model_id: str,
            storage: Any,
            settings: Any,
            **kwargs: Any,
        ) -> ChatMessage:
            del agent, summary_adapter, summary_model_id, storage, settings

            self.compacted = True
            self.compact_calls += 1
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

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "reasoning": "Need weather.",
                "reasoning_meta": {"encrypted_content": "opaque-current-turn"},
                "usage": {"input_tokens": 11, "output_tokens": 7},
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Sunny", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
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
    register_history_tool(runtime.tools, runtime.chat_sessions)
    compaction_service = SingleCheckpointCompactionService()

    assistant = await build_chat_loop(
        runtime,
        compaction_service=cast(Any, compaction_service),
    ).send("coder", "Weather?", session_id="session-one")

    continued_messages = adapter.requests[1]["messages"]
    first_tool_names = [tool["name"] for tool in adapter.requests[0]["kwargs"]["tools"]]
    continued_tool_names = [tool["name"] for tool in adapter.requests[1]["kwargs"]["tools"]]
    assert assistant.content == "Sunny"
    assert compaction_service.compact_calls == 1
    assert HISTORY_TOOL_NAME not in first_tool_names
    assert HISTORY_TOOL_NAME in continued_tool_names
    assert [message["role"] for message in continued_messages] == [
        "system",
        "user",
        "user",
        "assistant",
        "tool",
    ]
    assert continued_messages[1]["content"] == (
        "<system-reminder>\nCompacted prior context.\n\n"
        f"{HISTORY_COMPACTION_GUIDANCE.format(ordinal=1)}\n</system-reminder>"
    )
    assert continued_messages[3]["reasoning"] == "Need weather."
    assert continued_messages[3]["reasoning_meta"] == {"encrypted_content": "opaque-current-turn"}
    assert "usage" not in continued_messages[3]


@pytest.mark.asyncio
async def test_disallowed_tool_call_is_blocked_and_persisted_before_error(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({}),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "Weather?", session_id="session-one")

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert persisted_roles(messages) == ["user", "assistant", "tool", "assistant"]
    tool_message_content = messages[2].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == tool_failure(
        "tool_not_allowed",
        "Tool not allowed: get_weather",
    )


@pytest.mark.asyncio
async def test_registered_search_tools_execute_and_persist_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.tools.grep.shutil.which", lambda _command: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "code.py").write_text("print('alpha')\n", encoding="utf-8")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["glob", "grep"],
        workspace=workspace,
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_glob", "name": "glob", "arguments": {"pattern": "**/*.txt"}},
                    {"id": "call_grep", "name": "grep", "arguments": {"pattern": "alpha"}},
                ],
            },
            {"content": "Search complete", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    register_glob_tool(tools)
    register_grep_tool(tools)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send(
        "coder", "Search files", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    tool_messages = [message for message in messages if message.role == "tool"]
    glob_content = tool_messages[0].content
    grep_content = tool_messages[1].content
    assert isinstance(glob_content, str)
    assert isinstance(grep_content, str)
    glob_result = json.loads(glob_content)
    grep_result = json.loads(grep_content)
    assert assistant.content == "Search complete"
    assert [message.name for message in tool_messages] == ["glob", "grep"]
    assert glob_result == tool_success({"content": "notes.txt"})
    assert grep_result == tool_success(
        {"content": "notes.txt:1: alpha\nsrc/code.py:1: print('alpha')"}
    )
    assert [
        event.payload["tool_call"]["name"]
        for event in run.events
        if event.type == TOOL_CALL_STARTED_EVENT
    ] == ["glob", "grep"]
    for event in run.events:
        if event.type not in {TOOL_CALL_STARTED_EVENT, TOOL_CALL_RESULT_EVENT}:
            continue
        tool_name = event.payload["tool_call"]["name"]
        assert event.payload["schema_fingerprint"] == tools.schema_fingerprint(tool_name)
    results_by_tool = {
        event.payload["tool_call"]["name"]: event.payload["result"]
        for event in run.events
        if event.type == TOOL_CALL_RESULT_EVENT
    }
    assert results_by_tool == {"glob": glob_result, "grep": grep_result}


@pytest.mark.asyncio
async def test_registered_search_tools_respect_agent_allowlist(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("alpha\n", encoding="utf-8")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["glob"],
        workspace=workspace,
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_grep", "name": "grep", "arguments": {"pattern": "alpha"}}
                ],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    register_glob_tool(tools)
    register_grep_tool(tools)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "Search files", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    failure = tool_failure("tool_not_allowed", "Tool not allowed: grep")
    tool_message_content = messages[2].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == failure
    result_payload = next(
        event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT
    ).payload
    assert result_payload["tool_call"] == {"id": "call_grep", "index": 0, "name": "grep"}
    assert result_payload["result"] == failure
    assert result_payload["timing"]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_same_turn_tool_calls_run_concurrently_and_persist_in_call_order(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["slow"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "slow", "arguments": {"value": "first"}},
                    {"id": "call_2", "name": "slow", "arguments": {"value": "second"}},
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    second_started = asyncio.Event()
    first_can_finish = asyncio.Event()

    async def slow_handler(context: ToolContext, arguments: ToolJsonObject) -> ToolJsonObject:
        if context.tool_call_id == "call_1":
            await second_started.wait()
            first_can_finish.set()
        else:
            second_started.set()
        return tool_success({"value": arguments["value"], "id": context.tool_call_id})

    tools = ToolRegistry()
    tools.register(
        "slow",
        "Slow tool.",
        {"type": "object"},
        slow_handler,
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send("coder", "Run tools", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    result_events = [event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT]
    assert assistant.content == "Done"
    assert first_can_finish.is_set()
    assert [message.tool_call_id for message in messages if message.role == "tool"] == [
        "call_1",
        "call_2",
    ]
    assert [event.payload["tool_call"]["id"] for event in result_events] == ["call_2", "call_1"]
    tool_result_ids: list[str] = []
    for message in messages:
        if message.role != "tool":
            continue
        assert isinstance(message.content, str)
        tool_result_ids.append(json.loads(message.content)["data"]["id"])
    assert tool_result_ids == [
        "call_1",
        "call_2",
    ]


@pytest.mark.asyncio
async def test_same_tool_sibling_calls_run_in_parallel(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["same"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "same", "arguments": {}},
                    {"id": "call_2", "name": "same", "arguments": {}},
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    active_count = 0
    max_active_count = 0
    release = asyncio.Event()

    async def same_handler(context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        nonlocal active_count, max_active_count
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        if max_active_count == 2:
            release.set()
        await release.wait()
        active_count -= 1
        return tool_success({"id": context.tool_call_id})

    tools = ToolRegistry()
    tools.register(
        "same",
        "Same tool.",
        {"type": "object"},
        same_handler,
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "Run tools", session_id="session-one")

    assert max_active_count == 2


@pytest.mark.asyncio
async def test_tool_handler_exception_continues_with_failure_envelope(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["explode"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "explode", "arguments": {}}],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )

    def failing_handler(_context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        raise RuntimeError("boom")

    tools = ToolRegistry()
    tools.register("explode", "Explode.", {"type": "object"}, failing_handler)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send("coder", "Run tool", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert assistant.content == "Recovered"
    assert run.status == RunStatus.COMPLETED
    tool_message_content = messages[2].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == tool_failure("tool_execution_error", "boom")
    result_payload = next(
        event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT
    ).payload
    assert result_payload["tool_call"] == {"id": "call_1", "index": 0, "name": "explode"}
    assert result_payload["result"] == tool_failure("tool_execution_error", "boom")
    assert result_payload["timing"]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_invalid_arguments_can_be_corrected_without_running_handler_twice(
    tmp_path: Path,
) -> None:
    invocations: list[ToolJsonObject] = []

    def handler(_context: ToolContext, arguments: ToolJsonObject) -> ToolJsonObject:
        invocations.append(arguments)
        return tool_success({"city": arguments["city"]})

    tools = ToolRegistry()
    tools.register(
        "weather",
        "Read weather for one city.",
        {
            "type": "object",
            "properties": {"city": {"type": "string", "minLength": 1}},
            "required": ["city"],
            "additionalProperties": False,
        },
        handler,
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "weather", "arguments": {"city": 7}}],
            },
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_2", "name": "weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send(
        "coder",
        "Check the weather",
        session_id="session-one",
    )

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    tool_results = [
        json.loads(cast(str, message.content)) for message in messages if message.role == "tool"
    ]
    assert assistant.content == "Recovered"
    assert invocations == [{"city": "Berlin"}]
    assert tool_results[0]["error"]["code"] == "invalid_arguments"
    assert "arguments/city" in tool_results[0]["error"]["message"]
    assert tool_results[1] == tool_success({"city": "Berlin"})


@pytest.mark.asyncio
async def test_output_truncated_tool_calls_persist_failures_without_handler_side_effects(
    tmp_path: Path,
) -> None:
    invocations: list[ToolJsonObject] = []

    def write_handler(
        _context: ToolContext,
        arguments: ToolJsonObject,
    ) -> ToolJsonObject:
        invocations.append(arguments)
        return tool_success({"written": arguments["path"]})

    tools = ToolRegistry()
    tools.register(
        "write_probe",
        "Write a probe file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        write_handler,
    )
    adapter = StubAdapter(
        [
            {
                "content": "I started preparing both writes.",
                "terminal_outcome": "output_truncated",
                "tool_calls": [
                    {
                        "id": "call_first",
                        "name": "write_probe",
                        "arguments": {"path": "first.txt"},
                    },
                    {
                        "id": "call_second",
                        "name": "write_probe",
                        "arguments": {"path": "second.txt", "content": ""},
                    },
                ],
            },
            {"content": "I will reissue complete calls if needed.", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(
            id="coder",
            model="openai/gpt-5.2",
            allowed_tools=["write_probe"],
        ),
        adapter=adapter,
        tools=tools,
    )

    assistant = await build_chat_loop(runtime).send(
        "coder",
        "Write both files",
        session_id="session-one",
    )

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    tool_messages = [message for message in messages if message.role == "tool"]
    assert assistant.content == "I will reissue complete calls if needed."
    assert invocations == []
    assert [message.tool_call_id for message in tool_messages] == [
        "call_first",
        "call_second",
    ]
    assert [
        json.loads(cast(str, message.content))["error"]["code"] for message in tool_messages
    ] == [
        "tool_call_truncated",
        "tool_call_truncated",
    ]
    assert [message["tool_call_id"] for message in adapter.requests[1]["messages"][-2:]] == [
        "call_first",
        "call_second",
    ]
    assert messages[1].content == "I started preparing both writes."
    assert persisted_roles(messages) == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_outcome", ["content_filtered", "error", "unknown"])
async def test_unsafe_terminal_outcome_fails_closed_before_tool_handler(
    tmp_path: Path,
    terminal_outcome: str,
) -> None:
    invocation_count = 0

    def handler(_context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        nonlocal invocation_count
        invocation_count += 1
        return tool_success({})

    tools = ToolRegistry()
    tools.register("probe", "Probe.", {"type": "object"}, handler)
    adapter = StubAdapter(
        [
            {
                "content": "unsafe partial",
                "terminal_outcome": terminal_outcome,
                "tool_calls": [{"id": "call_probe", "name": "probe", "arguments": {}}],
            }
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(
            id="coder",
            model="openai/gpt-5.2",
            allowed_tools=["probe"],
        ),
        adapter=adapter,
        tools=tools,
    )

    with pytest.raises(ProviderError, match="unsafe terminal outcome"):
        await build_chat_loop(runtime).send(
            "coder",
            "Run probe",
            session_id="session-one",
        )

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert invocation_count == 0
    assert persisted_roles(messages) == ["user", "assistant", "tool", "error"]
    assert json.loads(cast(str, messages[2].content))["error"]["code"] == "tool_call_rejected"


@pytest.mark.asyncio
async def test_tool_non_envelope_result_is_failure_envelope(tmp_path: Path) -> None:
    async def invalid_handler(
        _context: ToolContext,
        _arguments: ToolJsonObject,
    ) -> JsonObject:
        return {"content": "not enveloped"}

    tools = ToolRegistry()
    tools.register("invalid", "Invalid tool.", {"type": "object"}, invalid_handler)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["invalid"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "invalid", "arguments": {}}],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
    )

    assistant = await build_chat_loop(runtime).send(
        "coder", "Run invalid", session_id="session-one"
    )

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    failure = tool_failure(
        "invalid_tool_result",
        "Tool handler must return a valid result envelope: invalid",
    )
    assert assistant.content == "Recovered"
    tool_message_content = messages[2].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == failure
    run = next(iter(runtime.chat_runs._runs.values()))
    result_payload = next(
        event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT
    ).payload
    assert result_payload["tool_call"] == {"id": "call_1", "index": 0, "name": "invalid"}
    assert result_payload["result"] == failure
    assert result_payload["timing"]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_max_tool_iteration_stop_raises_chat_error(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            }
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({}),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    with pytest.raises(ChatError, match="maximum tool iterations"):
        await build_chat_loop(runtime, max_tool_iterations=0).send(
            "coder",
            "Weather?",
            session_id="session-one",
        )

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert persisted_roles(messages) == ["user", "assistant", "error"]
    assert messages[2].error_kind == "tool_iterations_exceeded"


def test_failed_tool_call_circuit_breaker_resets_on_change_and_success() -> None:
    breaker = _FailedToolCallCircuitBreaker()
    berlin = ToolCall(id="call-berlin", name="weather", arguments={"city": "Berlin"})
    paris = ToolCall(id="call-paris", name="weather", arguments={"city": "Paris"})

    def result_message(tool_call: ToolCall, *, ok: bool) -> ChatMessage:
        result = tool_success({}) if ok else tool_failure("weather_error", "unavailable")
        return ChatMessage.tool(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=json.dumps(result),
        )

    for _ in range(MAX_IDENTICAL_FAILED_TOOL_CALLS - 1):
        assert breaker.observe([berlin], [result_message(berlin, ok=False)]) is None

    assert breaker.observe([paris], [result_message(paris, ok=False)]) is None
    assert breaker.observe([paris], [result_message(paris, ok=True)]) is None

    for _ in range(MAX_IDENTICAL_FAILED_TOOL_CALLS - 1):
        assert breaker.observe([paris], [result_message(paris, ok=False)]) is None
    assert breaker.observe([paris], [result_message(paris, ok=False)]) == "weather"


def test_failed_tool_call_circuit_breaker_keys_error_class_and_schema_version() -> None:
    breaker = _FailedToolCallCircuitBreaker(limit=2)
    call = ToolCall(id="call", name="weather", arguments={"city": "Berlin"})

    class Registry:
        fingerprint = "schema-v1"

        def schema_fingerprint(self, name: str) -> str:
            assert name == "weather"
            return self.fingerprint

    registry = Registry()

    def failed(code: str) -> ChatMessage:
        return ChatMessage.tool(
            tool_call_id=call.id,
            name=call.name,
            content=json.dumps(tool_failure(code, "failed")),
        )

    assert breaker.observe([call], [failed("timeout")], registry) is None
    assert breaker.observe([call], [failed("permission_denied")], registry) is None
    registry.fingerprint = "schema-v2"
    assert breaker.observe([call], [failed("permission_denied")], registry) is None
    assert breaker.observe([call], [failed("permission_denied")], registry) == "weather"


@pytest.mark.asyncio
async def test_identical_failed_tool_call_stops_run_on_eighth_call(tmp_path: Path) -> None:
    invocation_count = 0

    def failing_handler(
        _context: ToolContext,
        _arguments: ToolJsonObject,
    ) -> JsonObject:
        nonlocal invocation_count
        invocation_count += 1
        return tool_failure("invalid_arguments", "name is required")

    repeated_call = {
        "name": "skill_manage",
        "arguments": {"action": "create", "name": "demo"},
    }
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": f"call_{index}", **repeated_call}],
            }
            for index in range(MAX_IDENTICAL_FAILED_TOOL_CALLS)
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "skill_manage",
        "Manage Skills.",
        {"type": "object"},
        failing_handler,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(
            id="coder",
            model="openai/gpt-5.2",
            allowed_tools=["skill_manage"],
        ),
        adapter=adapter,
        tools=tools,
    )

    with pytest.raises(ChatError, match="repeated the same failed call 8 times"):
        await build_chat_loop(runtime).send(
            "coder",
            "Create a Skill",
            session_id="session-one",
        )

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert invocation_count == MAX_IDENTICAL_FAILED_TOOL_CALLS
    assert len(adapter.requests) == MAX_IDENTICAL_FAILED_TOOL_CALLS
    assert messages[-2].role == "error"
    assert messages[-2].error_kind == "tool_iterations_exceeded"
    assert persisted_roles(messages) == [
        "user",
        *(["assistant", "tool"] * MAX_IDENTICAL_FAILED_TOOL_CALLS),
        "error",
    ]


@pytest.mark.asyncio
async def test_tool_iteration_limit_is_scoped_to_current_run(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_2", "name": "get_weather", "arguments": {"city": "Paris"}}
                ],
            },
            {"content": "First run done", "tool_calls": None},
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_3", "name": "get_weather", "arguments": {"city": "Rome"}}
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_4", "name": "get_weather", "arguments": {"city": "Madrid"}}
                ],
            },
            {"content": "Second run done", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"ok": True}),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    chat_loop = build_chat_loop(runtime, max_tool_iterations=2)

    first = await chat_loop.send("coder", "Weather batch one", session_id="session-one")
    second = await chat_loop.send("coder", "Weather batch two", session_id="session-one")

    assert first.content == "First run done"
    assert second.content == "Second run done"

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert all(message.role != "error" for message in messages)
    assert persisted_roles(messages) == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
