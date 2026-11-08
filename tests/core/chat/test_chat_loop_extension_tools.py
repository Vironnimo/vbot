"""Extension Tools that change during a Run, and bound Session capabilities."""

import asyncio
import json
import threading
from typing import Any

import pytest

from core.agents.temporary import TemporaryAgentConfig, TemporaryAgentRegistry
from core.extensions import (
    ExtensionAPI,
    ExtensionRecord,
    ExtensionRegistry,
    PreparedSessionDelivery,
    ToolBatchDecision,
)
from core.extensions.extensions import ExtensionDeclarations
from core.extensions.operations import ExtensionOperations
from core.providers.errors import NetworkError, ProviderStreamingUnsupportedError
from core.runs import TOOL_CALL_RESULT_EVENT, RunCancelledError, RunExecutionOwner
from core.sessions import SessionAddress
from core.tools import ToolRegistry, tool_success
from core.tools.availability import ToolAccess
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop
from tests.core.chat.chat_loop_tools_test_support import WAIT_SECONDS


def _value_schema(value_type: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"value": {"type": value_type}},
        "required": ["value"],
        "additionalProperties": False,
    }


def _dynamic_tool(handler: Any, value_type: str = "string") -> dict[str, Any]:
    return {
        "name": "dynamic",
        "description": "test-sentinel",
        "parameters": {"type": "object"},
        "handler": handler,
        "result_schema": _value_schema(value_type),
    }


def _observed_runtime(
    tmp_path: Any, tools: ToolRegistry, responses: list[dict[str, Any]], result_hook: Any
) -> Any:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=StubAdapter(responses), tools=tools
    )
    extensions = ExtensionRegistry()
    extensions.install_handler("observer", "tool_result", result_hook)
    runtime.extensions = extensions
    runtime.chat_sessions.create("coder", session_id="session-one")
    return runtime


async def _tool_results(runtime: Any, run: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the persisted and the streamed Tool results by Tool-call id."""
    history = runtime.chat_sessions.get(SessionAddress(None, "coder", "session-one")).load()
    persisted = {
        message.tool_call_id: json.loads(message.content)
        for message in history
        if message.role == "tool"
    }
    streamed = {
        event.payload["tool_call"]["id"]: event.payload["result"]
        for event in await runtime.timelines.events(run)
        if event.type == TOOL_CALL_RESULT_EVENT
    }
    return persisted, streamed


@pytest.mark.asyncio
@pytest.mark.parametrize("programmatic", [False, True])
@pytest.mark.parametrize("descendant", [False, True])
async def test_owned_tool_budget_refuses_effects_and_stops_model_continuation(
    tmp_path, programmatic, descendant
):
    tools = ToolRegistry()
    declarations = ExtensionDeclarations()
    api = ExtensionAPI("private", declarations, config={}, logger=None)
    effects = []
    admitted = []

    async def effect(context, _arguments):
        effects.append(context.tool_call_id)
        return tool_success({"applied": True})

    async def program(context, _arguments):
        await context.call_tool("effect", {})
        return await context.call_tool("effect", {})

    async def guard(context, *, tool_name, tool_call_id):
        assert context.binding.group_id == "group"
        if tool_name != "program":
            if admitted:
                return "This Tool call was not run: the test-owned limit was reached."
            admitted.append(tool_call_id)
        return None

    register = api.register_tool if descendant else api.register_session_tool
    register("effect", "test-owned effect", {"type": "object"}, effect)
    register("program", "test-owned program", {"type": "object"}, program)
    if descendant:
        api.register_session_tool(
            "private_finish", "test-owned Tool", {"type": "object"}, lambda *_args: tool_success({})
        )
    api.register_session_runtime(
        before_request=lambda _context: None,
        before_tool_call=guard,
        run_finished=lambda _context, **_kwargs: None,
        quiesce=lambda: None,
    )
    extensions = ExtensionRegistry()
    extensions._records.append(  # noqa: SLF001 - focused live-registry fixture
        ExtensionRecord(
            "private", tmp_path, tmp_path / "extension.py", "loaded", declarations=declarations
        )
    )
    extensions.apply_tools(tools)
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": (
                    [{"id": "program-call", "name": "program", "arguments": {}}]
                    if programmatic
                    else [
                        {"id": f"effect-{i}", "name": "effect", "arguments": {}} for i in range(3)
                    ]
                ),
            }
        ]
    )
    agent = StubAgent(id="ordinary", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.extensions = extensions
    temporary = TemporaryAgentRegistry(runtime.chat_sessions)
    runtime.agent_resolver.temporary_agents = temporary
    binding = temporary.create(
        owner_name="private",
        group_id="group",
        participant_id="participant",
        config=TemporaryAgentConfig(
            model="openai/gpt-5.2",
            cwd=tmp_path,
            tool_access=ToolAccess(mode="selected", allowed=()),
            allowed_skills=[],
            tools={},
            name="test",
        ),
    )
    loop = build_chat_loop(runtime)
    if descendant:
        child = runtime.chat_sessions.create("ordinary", session_id="child")
        owner = RunExecutionOwner("private", "group", "participant", binding.generation_id, "epoch")
        run = await loop.start_owned_continuation(child.address, owner, "initial", "child-input")
    else:
        run = await loop.start_temporary_run(binding, "initial")
    await run.wait()
    assert effects == admitted and len(effects) == 1
    assert len(adapter.requests) == 1
    persisted = [
        json.loads(message.content)
        for message in runtime.chat_sessions.get(
            SessionAddress(run.project_id, run.agent_id, run.session_id)
        ).load()
        if message.role == "tool"
    ]
    refusals = [value for value in persisted if value.get("error")]
    assert len(refusals) == 2
    assert all(value["error"]["code"] == "execution_paused" for value in refusals)
    if descendant:
        assert "private_finish" not in str(adapter.requests[0]["kwargs"].get("tools"))
    assert run.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "descendant"),
    [
        ("compaction", False),
        ("retry", False),
        ("non_streaming", False),
        ("compaction", True),
        ("retry", True),
        ("before_compaction", True),
    ],
)
async def test_owned_model_admission_follows_compaction_and_each_physical_attempt(
    tmp_path, monkeypatch, boundary, descendant
):
    tools = ToolRegistry()
    declarations = ExtensionDeclarations()
    api = ExtensionAPI("private", declarations, config={}, logger=None)
    delivery = PreparedSessionDelivery("notice", "hash", ("Close out the work.",), "1")
    deliveries = []
    acknowledgments = []
    admissions = []
    compacted = False
    adapter = StubAdapter(
        [],
        stream_responses=[
            ProviderStreamingUnsupportedError("Streaming unavailable")
            if boundary == "non_streaming"
            else NetworkError("Connection interrupted")
        ],
    )

    async def before_request(_context):
        deliveries.append(delivery)
        return delivery

    async def before_model_request(context):
        assert context.binding.group_id == "group"
        admissions.append((compacted, len(adapter.stream_requests)))
        if boundary == "before_compaction" or compacted or adapter.stream_requests:
            raise RuntimeError("Owned execution limit reached")

    async def compact(context, *_args, **_kwargs):
        nonlocal compacted
        compacted = True
        return context.request_state

    api.register_session_tool(
        "finish", "test-owned Tool", {"type": "object"}, lambda *_args: tool_success({})
    )
    api.register_session_runtime(
        before_request=before_request,
        before_model_request=before_model_request,
        acknowledge_delivery=lambda _context, prepared: acknowledgments.append(prepared),
        run_finished=lambda _context, **_kwargs: None,
        quiesce=lambda: None,
    )
    extensions = ExtensionRegistry()
    extensions._records.append(  # noqa: SLF001 - focused live-registry fixture
        ExtensionRecord(
            "private", tmp_path, tmp_path / "extension.py", "loaded", declarations=declarations
        )
    )
    extensions.apply_tools(tools)
    agent = StubAgent(id="ordinary", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.extensions = extensions
    temporary = TemporaryAgentRegistry(runtime.chat_sessions)
    runtime.agent_resolver.temporary_agents = temporary
    binding = temporary.create(
        owner_name="private",
        group_id="group",
        participant_id="participant",
        config=TemporaryAgentConfig(
            model="openai/gpt-5.2",
            cwd=tmp_path,
            tool_access=ToolAccess(mode="selected", allowed=()),
            allowed_skills=[],
            tools={},
            name="test",
        ),
    )
    service: Any = object() if "compaction" in boundary else None
    loop = build_chat_loop(runtime, compaction_service=service)
    monkeypatch.setattr(loop._compaction_runs, "maybe_auto_compact_state", compact)
    if descendant:
        child = runtime.chat_sessions.create("ordinary", session_id="child")
        owner = RunExecutionOwner("private", "group", "participant", binding.generation_id, "epoch")
        run = await loop.start_owned_continuation(child.address, owner, "initial", "child-input")
    else:
        run = await loop.start_temporary_run(binding, "initial")
    with pytest.raises(RuntimeError, match="Owned execution limit reached"):
        await run.wait()

    expected = [(True, 0)] if boundary == "compaction" else [(False, 0), (False, 1)]
    if descendant:
        expected.insert(0, (False, 0))
    if boundary == "before_compaction":
        expected = [(False, 0)]
        assert not compacted
    assert admissions == expected
    assert len(adapter.stream_requests) == (0 if "compaction" in boundary else 1)
    assert adapter.requests == []
    assert deliveries == acknowledgments == ([] if descendant else [delivery])
    notes = [
        m.content
        for m in runtime.chat_sessions.get(
            SessionAddress(run.project_id, run.agent_id, run.session_id)
        ).load()
        if m.role == "note"
    ]
    assert notes.count("Close out the work.") == (0 if descendant else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("catalog_change", "valid_hook_result"),
    [("replace", False), ("replace", True), ("remove", True)],
)
async def test_running_tool_result_survives_live_catalog_change(
    tmp_path, catalog_change, valid_hook_result
):
    tools = ToolRegistry()
    operations = ExtensionOperations("test")
    operations.bind(tools)
    started = asyncio.Event()
    resume = asyncio.Event()
    effects: list[str] = []

    async def original(_context, _arguments):
        started.set()
        await resume.wait()
        effects.append("completed")
        return tool_success({"value": "completed"})

    operations.replace_tools("catalog", [_dynamic_tool(original)])
    hook_results: list[dict[str, Any]] = []

    def result_hook(_context, **payload):
        hook_results.append(payload["result"])
        return tool_success({"value": "completed and observed" if valid_hook_result else 1})

    runtime = _observed_runtime(
        tmp_path,
        tools,
        [
            {
                "content": None,
                "tool_calls": [{"id": "dynamic-call", "name": "dynamic", "arguments": {}}],
            },
            {"content": "finished"},
        ],
        result_hook,
    )
    run = await build_chat_loop(runtime).start_run(
        "coder", "run the Tool", session_id="session-one"
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=WAIT_SECONDS)
        # The running call keeps the result contract it was dispatched with.
        candidates = [_dynamic_tool(original, "integer")] if catalog_change == "replace" else []
        operations.replace_tools("catalog", candidates)
    finally:
        resume.set()
        await run.wait()

    expected = tool_success(
        {"value": "completed and observed" if valid_hook_result else "completed"}
    )
    assert effects == ["completed"]
    assert hook_results == [tool_success({"value": "completed"})]
    assert await _tool_results(runtime, run) == (
        {"dynamic-call": expected},
        {"dynamic-call": expected},
    )
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_run_cancel_lets_result_hooks_of_a_finished_call_complete(tmp_path):
    hook_running = asyncio.Event()
    release = asyncio.Event()

    async def result_hook(_context, **payload):
        hook_running.set()
        await release.wait()
        return tool_success({"value": "completed and observed"})

    tools = ToolRegistry()
    tools.register(
        "probe", "Complete at once.", {"type": "object"}, lambda _c, _a: tool_success({})
    )
    runtime = _observed_runtime(
        tmp_path,
        tools,
        [{"content": None, "tool_calls": [{"id": "probe-call", "name": "probe", "arguments": {}}]}],
        result_hook,
    )
    run = await build_chat_loop(runtime).start_run("coder", "Probe", session_id="session-one")
    await asyncio.wait_for(hook_running.wait(), timeout=WAIT_SECONDS)

    run.request_cancel(reason="user")
    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()

    persisted, _streamed = await _tool_results(runtime, run)
    assert persisted == {"probe-call": tool_success({"value": "completed and observed"})}


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_hook_result", [False, True])
async def test_worker_publication_before_dispatch_uses_selected_tool_contract(
    tmp_path, monkeypatch, valid_hook_result
):
    tools = ToolRegistry()
    operations = ExtensionOperations("test")
    operations.bind(tools)
    publish = threading.Event()
    published = threading.Event()
    effects: list[str] = []
    publisher_threads: list[int] = []
    main_thread = threading.get_ident()

    async def original(_context, _arguments):
        effects.append("original")
        return tool_success({"value": "original"})

    async def replacement(_context, _arguments):
        effects.append("replacement")
        return tool_success({"value": 42})

    operations.replace_tools("catalog", [_dynamic_tool(original)])

    def publisher(_context, _arguments):
        publisher_threads.append(threading.get_ident())
        assert publish.wait(timeout=WAIT_SECONDS)
        try:
            operations.replace_tools("catalog", [_dynamic_tool(replacement, "integer")])
        finally:
            published.set()
        return tool_success({"published": True})

    operations.replace_tools(
        "publisher",
        [
            {
                "name": "publish",
                "description": "test-sentinel",
                "parameters": {"type": "object"},
                "handler": publisher,
            }
        ],
    )
    dispatch = tools.dispatch

    async def dispatch_after_publication(context, arguments, allowed_tools=None):
        if context.tool_name == "dynamic":
            # Force a real sibling worker publication after Chat has entered
            # dispatch but before canonical dispatch selects the executing Tool.
            publish.set()
            assert await asyncio.to_thread(published.wait, WAIT_SECONDS)
        return await dispatch(context, arguments, allowed_tools)

    monkeypatch.setattr(tools, "dispatch", dispatch_after_publication)
    hook_results: list[dict[str, Any]] = []

    def result_hook(_context, **payload):
        if payload["tool_name"] != "dynamic":
            return None
        hook_results.append(payload["result"])
        return tool_success({"value": 43 if valid_hook_result else "invalid"})

    runtime = _observed_runtime(
        tmp_path,
        tools,
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "publish-call", "name": "publish", "arguments": {}},
                    {"id": "dynamic-call", "name": "dynamic", "arguments": {}},
                ],
            },
            {"content": "finished"},
        ],
        result_hook,
    )
    run = await build_chat_loop(runtime).start_run(
        "coder", "run the Tools", session_id="session-one"
    )
    await run.wait()

    expected = tool_success({"value": 43 if valid_hook_result else 42})
    assert effects == ["replacement"]
    assert len(publisher_threads) == 1 and publisher_threads[0] != main_thread
    assert hook_results == [tool_success({"value": 42})]
    persisted, streamed = await _tool_results(runtime, run)
    assert persisted == {
        "publish-call": tool_success({"published": True}),
        "dynamic-call": expected,
    }
    assert streamed["dynamic-call"] == expected
    assert run.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("continue_after_batch", "finalization_failure", "programmatic"),
    [
        (True, None, False),
        (False, "summary", False),
        (False, "callback", False),
        (True, None, True),
    ],
)
async def test_bound_session_capability_delivers_once_and_ends_after_tool_batch(
    tmp_path, continue_after_batch, finalization_failure, programmatic, monkeypatch
):
    tools = ToolRegistry()
    declarations = ExtensionDeclarations()
    api = ExtensionAPI("private", declarations, config={}, logger=None)
    finished: list[str] = []
    private_call_ids: list[str] = []

    async def hidden(context, arguments):
        del arguments
        private_call_ids.append(context.tool_call_id)
        context.record_delivery_receipt("tool-receipt", "tool-hash", "inbox")
        context.record_delivery_receipt("second-receipt", "second-hash", "inbox")
        context.add_note("deferred-tool-note")
        context.request_turn_end()
        return tool_success({"sentinel": True})

    async def sibling(_context, _arguments):
        return tool_success({"sibling": True})

    async def program(context, _arguments):
        result = await context.call_tool("private_tool", {})
        receipt = await runtime.chat_sessions.lookup_delivery_receipt(
            binding.address, binding.generation_id, binding.owner_name, "tool-receipt"
        )
        assert receipt is not None
        return result

    async def reconcile(_context, *, receipts, persisted_call_ids, turn_end_requested):
        assert turn_end_requested
        assert persisted_call_ids == (
            (private_call_ids[0], "program-call", "sibling-call")
            if programmatic
            else ("private-call", "sibling-call")
        )
        assert len(receipts) == 2
        return ToolBatchDecision(
            end=not continue_after_batch,
            continuation=PreparedSessionDelivery(
                "continuation",
                "continuation-hash",
                ("continuation-fixture",),
                "test-revision",
            )
            if continue_after_batch
            else None,
        )

    async def before_request(_context):
        return PreparedSessionDelivery(
            delivery_id="request-receipt",
            content_hash="request-hash",
            entries=("test-delivery-sentinel",),
            settings_revision="test-revision",
        )

    async def run_finished(_context, *, outcome):
        finished.append(outcome)
        if finalization_failure == "callback":
            raise OSError("callback-fixture")

    api.register_session_tool(
        "private_tool",
        "test-sentinel",
        {"type": "object", "properties": {}, "additionalProperties": False},
        hidden,
    )
    api.register_session_prompt_block("private", render=lambda _binding: "test-prompt-sentinel")
    api.register_session_tool(
        "sibling_tool",
        "test-sentinel",
        {"type": "object", "properties": {}},
        sibling,
    )
    if programmatic:
        api.register_session_tool("program_tool", "test-sentinel", {"type": "object"}, program)
    api.register_session_runtime(
        before_request=before_request,
        run_finished=run_finished,
        quiesce=lambda: None,
        reconcile_tool_batch=reconcile,
    )
    extensions = ExtensionRegistry()
    extensions._records.append(  # noqa: SLF001 - focused live-registry fixture
        ExtensionRecord(
            "private", tmp_path, tmp_path / "extension.py", "loaded", declarations=declarations
        )
    )
    extensions.apply_tools(tools)

    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "program-call" if programmatic else "private-call",
                        "name": "program_tool" if programmatic else "private_tool",
                        "arguments": {},
                    },
                    {"id": "sibling-call", "name": "sibling_tool", "arguments": {}},
                ],
            }
        ]
        + ([{"content": "continued result"}] if continue_after_batch else [])
    )
    agent = StubAgent(id="ordinary", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.extensions = extensions
    temporary = TemporaryAgentRegistry(runtime.chat_sessions)
    runtime.agent_resolver.temporary_agents = temporary
    binding = temporary.create(
        owner_name="private",
        group_id="group",
        participant_id="participant",
        config=TemporaryAgentConfig(
            model="openai/gpt-5.2",
            cwd=tmp_path,
            tool_access=ToolAccess(mode="selected", allowed=()),
            allowed_skills=["*"],
            tools={},
            name="test",
        ),
    )

    finish = runtime.chat_sessions.finish_run

    async def finish_with_failure(*args, **kwargs):
        if finalization_failure == "summary":
            raise OSError("summary-fixture")
        return await finish(*args, **kwargs)

    monkeypatch.setattr(runtime.chat_sessions, "finish_run", finish_with_failure)
    run = await build_chat_loop(runtime).start_temporary_run(binding, "initial")
    if finalization_failure == "summary":
        with pytest.raises(OSError):
            await run.wait()
    else:
        await run.wait()

    request = adapter.requests[0]
    assert {tool["name"] for tool in request["kwargs"]["tools"]} == {
        "private_tool",
        "sibling_tool",
        *(["program_tool"] if programmatic else []),
    }
    assert "test-prompt-sentinel" in request["messages"][0]["content"]
    assert "test-delivery-sentinel" in str(request["messages"])
    assert [
        message.content
        for message in runtime.chat_sessions.get(binding.address).load()
        if message.role == "note"
    ] == ["test-delivery-sentinel", "deferred-tool-note"] + (
        ["continuation-fixture"] if continue_after_batch else []
    )
    assert (
        await runtime.chat_sessions.lookup_delivery_receipt(
            binding.address, binding.generation_id, binding.owner_name, "request-receipt"
        )
        is not None
    )
    assert (
        await runtime.chat_sessions.lookup_delivery_receipt(
            binding.address, binding.generation_id, binding.owner_name, "tool-receipt"
        )
        is not None
    )
    assert finished == ["error" if finalization_failure == "summary" else "success"]
    if finalization_failure == "callback":
        assert run.events[-1].payload["completion_notification_errors"] == ["callback-fixture"]
    assert run.status == ("failed" if finalization_failure == "summary" else "completed")
    history = runtime.chat_sessions.get(binding.address).load()
    assert {message.tool_call_id for message in history if message.role == "tool"} == {
        *private_call_ids,
        *(["program-call"] if programmatic else []),
        "sibling-call",
    }
    receipts = [
        await runtime.chat_sessions.lookup_delivery_receipt(
            binding.address,
            binding.generation_id,
            binding.owner_name,
            receipt_id,
        )
        for receipt_id in ("tool-receipt", "second-receipt")
    ]
    assert receipts[0].carrier_location == receipts[1].carrier_location
    notes = runtime.chat_sessions.get(binding.address).load_active()
    assert sum(message.content == "deferred-tool-note" for message in notes) == 1
    if continue_after_batch:
        messages = adapter.requests[1]["messages"]
        assert all(message["role"] != "note" for message in messages)
        assert sum("test-delivery-sentinel" in str(message) for message in messages) == 1
        positions = [index for index, message in enumerate(messages) if message["role"] == "tool"]
        assert all(
            "continuation-fixture" not in str(message) for message in messages[: max(positions) + 1]
        )
        assert "continuation-fixture" in str(messages[max(positions) + 1 :])
