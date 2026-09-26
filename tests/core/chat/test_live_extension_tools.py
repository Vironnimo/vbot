"""A Tool installed during a Run is usable in its next Provider cycle."""

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
from core.runs import TOOL_CALL_RESULT_EVENT
from core.sessions import SessionAddress
from core.tools import ToolRegistry, tool_success
from core.tools.availability import ToolAccess
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_change", ["remove", "replace"])
@pytest.mark.parametrize("valid_hook_result", [False, True])
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

    declaration = {
        "name": "dynamic",
        "description": "test-sentinel",
        "parameters": {"type": "object"},
        "handler": original,
        "result_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    operations.replace_tools("catalog", [declaration])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "dynamic-call", "name": "dynamic", "arguments": {}}],
            },
            {"content": "finished"},
        ]
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    extensions = ExtensionRegistry()
    hook_results: list[dict[str, Any]] = []

    def result_hook(_context, **payload):
        hook_results.append(payload["result"])
        return tool_success({"value": "completed and observed" if valid_hook_result else 1})

    extensions.install_handler("observer", "tool_result", result_hook)
    runtime.extensions = extensions
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    run = await build_chat_loop(runtime).start_run("coder", "run the Tool", session_id=session.id)
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        candidates = []
        if catalog_change == "replace":
            candidates.append(
                {
                    **declaration,
                    "result_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                }
            )
        operations.replace_tools("catalog", candidates)
    finally:
        resume.set()
        await run.wait()

    expected = tool_success(
        {"value": "completed and observed" if valid_hook_result else "completed"}
    )
    assert effects == ["completed"]
    assert hook_results == [tool_success({"value": "completed"})]
    history = runtime.chat_sessions.get(SessionAddress(None, "coder", session.id)).load()
    assert [json.loads(message.content) for message in history if message.role == "tool"] == [
        expected
    ]
    assert [
        event.payload["result"] for event in run.events if event.type == TOOL_CALL_RESULT_EVENT
    ] == [expected]
    assert run.status == "completed"


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

    declaration = {
        "name": "dynamic",
        "description": "test-sentinel",
        "parameters": {"type": "object"},
        "handler": original,
        "result_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    operations.replace_tools("catalog", [declaration])

    def publisher(_context, _arguments):
        publisher_threads.append(threading.get_ident())
        assert publish.wait(timeout=5)
        try:
            operations.replace_tools(
                "catalog",
                [
                    {
                        **declaration,
                        "handler": replacement,
                        "result_schema": {
                            "type": "object",
                            "properties": {"value": {"type": "integer"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    }
                ],
            )
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
            assert await asyncio.to_thread(published.wait, 5)
        return await dispatch(context, arguments, allowed_tools)

    monkeypatch.setattr(tools, "dispatch", dispatch_after_publication)
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "publish-call", "name": "publish", "arguments": {}},
                    {"id": "dynamic-call", "name": "dynamic", "arguments": {}},
                ],
            },
            {"content": "finished"},
        ]
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    extensions = ExtensionRegistry()
    hook_results: list[dict[str, Any]] = []

    def result_hook(_context, **payload):
        if payload["tool_name"] != "dynamic":
            return None
        hook_results.append(payload["result"])
        return tool_success({"value": 43 if valid_hook_result else "invalid"})

    extensions.install_handler("observer", "tool_result", result_hook)
    runtime.extensions = extensions
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    run = await build_chat_loop(runtime).start_run("coder", "run the Tools", session_id=session.id)
    await run.wait()

    expected = tool_success({"value": 43 if valid_hook_result else 42})
    assert effects == ["replacement"]
    assert len(publisher_threads) == 1 and publisher_threads[0] != main_thread
    assert hook_results == [tool_success({"value": 42})]
    history = runtime.chat_sessions.get(SessionAddress(None, "coder", session.id)).load()
    assert {
        message.tool_call_id: json.loads(message.content)
        for message in history
        if message.role == "tool"
    } == {"publish-call": tool_success({"published": True}), "dynamic-call": expected}
    assert [
        event.payload["result"]
        for event in run.events
        if event.type == TOOL_CALL_RESULT_EVENT
        and event.payload["tool_call"]["id"] == "dynamic-call"
    ] == [expected]
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_live_catalog_publication_refreshes_next_provider_cycle(tmp_path):
    tools = ToolRegistry()
    operations = ExtensionOperations("test")
    operations.bind(tools)
    calls = []

    async def installed(context, arguments):
        calls.append("installed-called")
        return tool_success({"sentinel": True})

    async def install(context, arguments):
        operations.replace_tools(
            "connection",
            [
                {
                    "name": "installed",
                    "description": "test-sentinel",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "handler": installed,
                }
            ],
        )
        return tool_success({"installed": True})

    tools.register("install", "test-sentinel", {"type": "object"}, install)
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "install", "name": "install", "arguments": {}}],
            },
            {"content": None, "tool_calls": [{"id": "use", "name": "installed", "arguments": {}}]},
            {"content": "finished"},
        ]
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime).start_run(
        "coder", "install and use", session_id="session-one"
    )
    await run.wait()

    assert calls == ["installed-called"]
    assert "installed" not in {tool["name"] for tool in adapter.requests[0]["kwargs"]["tools"]}
    assert "installed" in {tool["name"] for tool in adapter.requests[1]["kwargs"]["tools"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("continue_after_batch", [False, True])
@pytest.mark.parametrize("finalization_failure", [None, "summary", "callback"])
async def test_bound_session_capability_delivers_once_and_ends_after_tool_batch(
    tmp_path, continue_after_batch, finalization_failure, monkeypatch
):
    tools = ToolRegistry()
    declarations = ExtensionDeclarations()
    api = ExtensionAPI("private", declarations, config={}, logger=None)
    finished: list[str] = []

    async def hidden(context, arguments):
        del arguments
        context.record_delivery_receipt("tool-receipt", "tool-hash", "inbox")
        context.record_delivery_receipt("second-receipt", "second-hash", "inbox")
        context.request_turn_end()
        return tool_success({"sentinel": True})

    async def sibling(_context, _arguments):
        return tool_success({"sibling": True})

    async def reconcile(_context, *, receipts, persisted_call_ids, turn_end_requested):
        assert turn_end_requested
        assert persisted_call_ids == ("private-call", "sibling-call")
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
                    {"id": "private-call", "name": "private_tool", "arguments": {}},
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

    async def finish_with_failure(*args):
        if finalization_failure == "summary":
            raise OSError("summary-fixture")
        return await finish(*args)

    monkeypatch.setattr(runtime.chat_sessions, "finish_run", finish_with_failure)
    run = await build_chat_loop(runtime).start_temporary_run(binding, "initial")
    if finalization_failure == "summary":
        with pytest.raises(OSError):
            await run.wait()
    else:
        await run.wait()

    request = adapter.requests[0]
    assert {tool["name"] for tool in request["kwargs"]["tools"]} == {"private_tool", "sibling_tool"}
    assert "test-prompt-sentinel" in request["messages"][0]["content"]
    assert "test-delivery-sentinel" in str(request["messages"])
    assert [
        message.content
        for message in runtime.chat_sessions.get(binding.address).load()
        if message.role == "note"
    ] == ["test-delivery-sentinel"] + (["continuation-fixture"] if continue_after_batch else [])
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
        "private-call",
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
    if continue_after_batch:
        messages = adapter.requests[1]["messages"]
        assert all(message["role"] != "note" for message in messages)
        assert sum("test-delivery-sentinel" in str(message) for message in messages) == 1
        positions = [index for index, message in enumerate(messages) if message["role"] == "tool"]
        assert all(
            "continuation-fixture" not in str(message) for message in messages[: max(positions) + 1]
        )
        assert "continuation-fixture" in str(messages[max(positions) + 1 :])
