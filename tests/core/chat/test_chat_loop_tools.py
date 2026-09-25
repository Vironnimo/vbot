"""Tests for chat loop tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
)
from core.model_tasks import TASK_IMAGE_UNDERSTANDING
from core.sessions import ChatSession
from core.sessions.store import SessionStore
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    BASH_SUBAGENT_TOOL_DESCRIPTION,
    BASH_SUBAGENT_TOOL_PARAMETERS,
    BASH_TOOL_DESCRIPTION,
    BASH_TOOL_NAME,
    BASH_TOOL_PARAMETERS,
    HISTORY_TOOL_NAME,
    ToolAccess,
    ToolContext,
    ToolRegistry,
    model_names,
    model_tool_name,
    register_history_tool,
    tool_success,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubModels,
    StubRuntime,
    StubStorage,
    build_chat_loop,
    persisted_roles,
    session_address,
)
from tests.core.chat.chat_loop_tools_test_support import (
    JsonObject,
)


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
async def test_sibling_tool_results_use_one_ordered_session_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = ToolRegistry()
    tools.register(
        "probe",
        "Return the probe id.",
        {"type": "object"},
        lambda context, _arguments: tool_success({"id": context.tool_call_id}),
        parallel_safe=True,
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["probe"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "first", "name": "probe", "arguments": {}},
                    {"id": "second", "name": "probe", "arguments": {}},
                ],
            },
            {"content": "done", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    batches: list[list[str]] = []
    original_append_many_async = ChatSession.append_many_async

    async def recording_append_many(
        self: ChatSession, messages: list[ChatMessage], **options: Any
    ) -> Any:
        batches.append([message.role for message in messages])
        return await original_append_many_async(self, messages, **options)

    monkeypatch.setattr(ChatSession, "append_many_async", recording_append_many)

    await build_chat_loop(runtime).send("coder", "run both", session_id="session-one")

    tool_batches = [batch for batch in batches if "tool" in batch]
    assert tool_batches == [["tool", "tool"]]
    persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert [message.tool_call_id for message in persisted if message.role == "tool"] == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_provider_requests_use_model_tool_names_while_the_session_keeps_registry_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_names, "_MODEL_NAMES", {"probe": "host_probe"})
    monkeypatch.setattr(model_names, "_REGISTRY_NAMES", {"host_probe": "probe"})
    dispatched: list[str] = []

    def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        dispatched.append(context.tool_name)
        return tool_success({"id": context.tool_call_id})

    tools = ToolRegistry()
    tools.register("probe", "Return the probe id.", {"type": "object"}, probe)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["probe"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "first", "name": "host_probe", "arguments": {}}],
            },
            {"content": "done", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "probe", session_id="session-one")

    assert dispatched == ["probe"]
    persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert [call.name for message in persisted for call in message.tool_calls or []] == ["probe"]
    follow_up = adapter.requests[1]
    assert [tool["name"] for tool in follow_up["kwargs"]["tools"]] == ["host_probe"]
    wire_calls = [
        call["name"]
        for message in follow_up["messages"]
        for call in message.get("tool_calls") or []
    ]
    assert wire_calls == ["host_probe"]
    assert [
        message.get("name") for message in follow_up["messages"] if message["role"] == "tool"
    ] == ["host_probe"]


@pytest.mark.asyncio
async def test_tool_called_by_another_harness_name_runs_and_is_stored_under_its_name(
    tmp_path: Path,
) -> None:
    dispatched: list[str] = []

    def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        dispatched.append(context.tool_name)
        return tool_success({"id": context.tool_call_id})

    tools = ToolRegistry()
    tools.register("web_fetch", "Fetch a page.", {"type": "object"}, probe)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["web_fetch"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "first", "name": "functions.WebFetch", "arguments": {}},
                    {"id": "second", "name": "TodoWrite", "arguments": {}},
                ],
            },
            {"content": "done", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "fetch", session_id="session-one")

    assert dispatched == ["web_fetch"]
    persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert [call.name for message in persisted for call in message.tool_calls or []] == [
        "web_fetch",
        "TodoWrite",
    ]
    unknown = next(
        message for message in persisted if message.role == "tool" and message.name == "TodoWrite"
    )
    assert "Unknown Tool: TodoWrite. Call one of the available Tools instead: web_fetch." in str(
        unknown.content
    )
    follow_up = adapter.requests[1]
    assert [
        call["name"]
        for message in follow_up["messages"]
        for call in message.get("tool_calls") or []
    ] == ["web_fetch", "TodoWrite"]


@pytest.mark.asyncio
async def test_ambiguous_tool_spelling_never_dispatches_a_harness_alias(tmp_path: Path) -> None:
    dispatched: list[str] = []

    def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        dispatched.append(context.tool_name)
        return tool_success({"id": context.tool_call_id})

    tools = ToolRegistry()
    offered = ["read", "read_file", "readfile"]
    for name in offered:
        tools.register(name, "Read a resource.", {"type": "object"}, probe)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=offered)
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "ambiguous", "name": "ReadFile", "arguments": {}}],
            },
            {"content": "done", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "read", session_id="session-one")

    assert dispatched == []
    persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert [call.name for message in persisted for call in message.tool_calls or []] == ["ReadFile"]
    result = next(message for message in persisted if message.role == "tool")
    assert json.loads(cast(str, result.content))["error"]["code"] == "tool_not_found"


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
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["probe"])

    class ObservingAdapter(StubAdapter):
        async def send(self, messages: Any, *, model_id: str, **kwargs: Any) -> Any:
            writes_at.setdefault("first_request", writes)
            return await super().send(messages, model_id=model_id, **kwargs)

    adapter = ObservingAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "first", "name": "probe", "arguments": {}}],
            },
            {"content": "done", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
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
    persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert persisted_roles(persisted)[-3:] == ["assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_nested_run_receives_non_handoff_bash_definition(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=[BASH_TOOL_NAME],
    )
    adapter = StubAdapter(
        [
            {"content": "top-level done", "tool_calls": None},
            {"content": "nested done", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        BASH_TOOL_NAME,
        BASH_TOOL_DESCRIPTION,
        BASH_TOOL_PARAMETERS,
        lambda _context, _arguments: tool_success({"status": "completed"}),
        open_input_schema=True,
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    parent = build_chat_loop(runtime)

    await parent.send("coder", "Top-level", session_id="top-level")
    await parent.child_loop(nesting_depth=1).send("coder", "Nested", session_id="nested")

    top_level_definition = adapter.requests[0]["kwargs"]["tools"][0]
    nested_definition = adapter.requests[1]["kwargs"]["tools"][0]
    # The Provider request carries the name the Model knows on this host, and the
    # description names no dedicated file Tool, since this Agent is offered none.
    usual_pointer = (
        "For reading, searching and editing files use read, search_files and apply_patch. "
    )
    assert top_level_definition == {
        "name": model_tool_name(BASH_TOOL_NAME),
        "description": BASH_TOOL_DESCRIPTION.replace(usual_pointer, ""),
        "parameters": BASH_TOOL_PARAMETERS,
    }
    assert nested_definition == {
        "name": model_tool_name(BASH_TOOL_NAME),
        "description": BASH_SUBAGENT_TOOL_DESCRIPTION.replace(usual_pointer, ""),
        "parameters": BASH_SUBAGENT_TOOL_PARAMETERS,
    }


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

    loop = build_chat_loop(runtime)
    preview = await loop.preview_tool_definitions(agent)
    assert ANALYZE_IMAGE_TOOL_NAME in {tool["name"] for tool in preview}
    assert not adapter.requests
    await loop.send("coder", "Inspect the image", session_id="s1")

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

    loop = build_chat_loop(runtime)
    preview = await loop.preview_tool_definitions(agent)
    assert ANALYZE_IMAGE_TOOL_NAME not in {tool["name"] for tool in preview}
    assert not adapter.requests
    await loop.send("coder", "Inspect the image", session_id="s1")

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
@pytest.mark.parametrize(
    ("policy", "available", "visible"),
    [
        (ToolAccess(mode="all"), True, False),
        (ToolAccess(mode="all", granted=(ANALYZE_IMAGE_TOOL_NAME,)), True, True),
        (
            ToolAccess(
                mode="selected",
                allowed=(ANALYZE_IMAGE_TOOL_NAME,),
                granted=(ANALYZE_IMAGE_TOOL_NAME,),
            ),
            True,
            True,
        ),
        (ToolAccess(mode="all", granted=(ANALYZE_IMAGE_TOOL_NAME,)), False, False),
        (ToolAccess(mode="none", granted=(ANALYZE_IMAGE_TOOL_NAME,)), True, False),
        (ToolAccess(mode="selected", granted=(ANALYZE_IMAGE_TOOL_NAME,)), True, False),
        (
            ToolAccess(
                mode="selected",
                denied=(ANALYZE_IMAGE_TOOL_NAME,),
                granted=(ANALYZE_IMAGE_TOOL_NAME,),
            ),
            True,
            False,
        ),
    ],
)
async def test_analyze_image_vision_grant_is_stable_and_respects_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: ToolAccess,
    available: bool,
    visible: bool,
) -> None:
    monkeypatch.setattr(StubAgent, "tool_access", property(lambda _self: policy))
    agent = StubAgent(id="coder", model="openai/vision-model")
    adapter = StubAdapter(
        [{"content": "done", "tool_calls": None}] * 2,
        wire_media_types=frozenset({"image/png"}),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=_analyze_image_registry(),
        models=StubModels(
            {("openai", "vision-model"): 128_000},
            input_modalities={("openai", "vision-model"): ("text", "image")},
        ),
        available_task_models={TASK_IMAGE_UNDERSTANDING} if available else set(),
    )
    loop = build_chat_loop(runtime)
    preview = await loop.preview_tool_definitions(agent)
    assert not adapter.requests
    assert (ANALYZE_IMAGE_TOOL_NAME in {tool["name"] for tool in preview}) is visible

    for message in ("Read the handwriting with analyze_image", "What is two plus two?"):
        await loop.send("coder", message, session_id="s1")
        assert (
            ANALYZE_IMAGE_TOOL_NAME in runtime.system_prompts.effective_tool_name_calls[-1]
        ) is visible

    assert adapter.requests[0]["kwargs"]["tools"] == preview
    assert adapter.requests[1]["kwargs"]["tools"] == preview


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
                    runtime_holder["runtime"]
                    .chat_sessions.get(session_address("coder", "session-one"))
                    .load()
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
            persisted_roles(
                runtime.chat_sessions.get(session_address("coder", "session-one")).load()
            )
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
            persisted_roles(
                runtime.chat_sessions.get(session_address("coder", "session-one")).load()
            )
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
            self.request_messages: list[JsonObject] = []
            self.checks = 0

        def estimate_messages_tokens(self, _messages: list[JsonObject]) -> int:
            return 90

        def has_new_compactable_context(
            self,
            _messages: list[ChatMessage],
            _settings: Any,
            **_kwargs: Any,
        ) -> bool:
            return True

        def should_auto_compact(
            self,
            _input_tokens: int,
            _context_window: int,
            _threshold: float,
            **_kwargs: Any,
        ) -> bool:
            self.checks += 1
            return self.checks == 2 and not self.compacted

        async def compact(
            self,
            messages: list[ChatMessage],
            *,
            session_address: Any,
            summary_adapter: Any,
            summary_model_id: str,
            storage: Any,
            settings: Any,
            **kwargs: Any,
        ) -> ChatMessage:
            del session_address, summary_adapter, summary_model_id, storage, settings

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
    assert compaction_service.request_messages[:2] == adapter.requests[0]["messages"]
    assert [message["role"] for message in compaction_service.request_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert compaction_service.request_messages[2]["reasoning"] == "Need weather."
    assert compaction_service.request_messages[3]["tool_call_id"] == "call_abc"
    assert HISTORY_TOOL_NAME not in first_tool_names
    assert HISTORY_TOOL_NAME in continued_tool_names
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
