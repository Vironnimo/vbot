"""Result payloads an Extension Tool attaches persist atomically with its Tool Result."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import pytest

from core.extensions.operations import ExtensionOperations
from core.runs import RunCancelledError
from core.sessions import SessionAddress
from core.tools import ToolContext, ToolRegistry, tool_failure, tool_success
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop

_ADDRESS = SessionAddress(None, "coder", "session-one")
_PARAMETERS = {"type": "object", "properties": {}, "additionalProperties": False}


def _runtime(tmp_path, tools: ToolRegistry, responses: list[dict[str, Any]]) -> Any:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=StubAdapter(responses), tools=tools
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    return runtime


def _extension_tools(**handlers: Any) -> ToolRegistry:
    """Tools the Extension ``owner`` publishes; each handler under its own name."""
    tools = ToolRegistry()
    operations = ExtensionOperations("owner")
    operations.bind(tools)
    operations.replace_tools(
        "catalog",
        [
            {"name": name, "description": "test-sentinel", "parameters": _PARAMETERS, "handler": h}
            for name, h in handlers.items()
        ],
    )
    return tools


def _call(name: str, call_id: str = "call-one") -> dict[str, Any]:
    return {"content": None, "tool_calls": [{"id": call_id, "name": name, "arguments": {}}]}


def _payload_rows(runtime: Any) -> list[tuple[Any, ...]]:
    with sqlite3.connect(runtime.chat_sessions._store.path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT c.call_id, p.payload_id, p.owner_name, p.payload_json "
                "FROM tool_result_payloads AS p JOIN tool_calls AS c ON c.call_key = p.call_key "
                "ORDER BY p.payload_key"
            )
        ]


def _tool_results(runtime: Any) -> dict[str, Any]:
    return {
        message.tool_call_id: json.loads(message.content)
        for message in runtime.chat_sessions.get(_ADDRESS).load()
        if message.role == "tool"
    }


@pytest.mark.asyncio
async def test_attached_payload_is_stored_with_its_tool_result(tmp_path) -> None:
    contexts: list[ToolContext] = []

    async def save(context: ToolContext, _arguments: Any) -> Any:
        contexts.append(context)
        assert context.result_payloads_available
        first = context.attach_result_payload({"rows": [1, 2, 3]})
        second = context.attach_result_payload("second")
        return tool_success({"saved": [first, second]})

    runtime = _runtime(tmp_path, _extension_tools(save=save), [_call("save"), {"content": "ok"}])

    run = await build_chat_loop(runtime).start_run("coder", "save", session_id="session-one")
    await run.wait()

    first, second = _tool_results(runtime)["call-one"]["data"]["saved"]
    assert first.startswith("res_") and len(first) == len("res_") + 16
    assert _payload_rows(runtime) == [
        ("call-one", first, "owner", '{"rows":[1,2,3]}'),
        ("call-one", second, "owner", '"second"'),
    ]
    sessions = runtime.chat_sessions
    assert await sessions.tool_result_payload_async(_ADDRESS, first, owner_name="owner") == {
        "rows": [1, 2, 3]
    }
    assert await sessions.tool_result_payload_async(_ADDRESS, first, owner_name="other") is None
    # The call is over: a late attach cannot reach history any more.
    with pytest.raises(RuntimeError, match="only while the Tool call runs"):
        contexts[0].attach_result_payload("late")


@pytest.mark.asyncio
async def test_a_failed_call_keeps_no_payload(tmp_path) -> None:
    async def crash(context: ToolContext, _arguments: Any) -> Any:
        context.attach_result_payload({"orphan": True})
        raise OSError("test-owned crash")

    async def invalid(context: ToolContext, _arguments: Any) -> Any:
        context.attach_result_payload({"orphan": True})
        return {"not": "an envelope"}

    async def failed(context: ToolContext, _arguments: Any) -> Any:
        # A failure the handler returns is its result; its payload stays readable.
        payload_id = context.attach_result_payload({"kept": True})
        return tool_failure("remote_error", payload_id)

    runtime = _runtime(
        tmp_path,
        _extension_tools(crash=crash, invalid=invalid, failed=failed),
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "crash-call", "name": "crash", "arguments": {}},
                    {"id": "invalid-call", "name": "invalid", "arguments": {}},
                    {"id": "failed-call", "name": "failed", "arguments": {}},
                ],
            },
            {"content": "ok"},
        ],
    )

    run = await build_chat_loop(runtime).start_run("coder", "fail", session_id="session-one")
    await run.wait()

    results = _tool_results(runtime)
    assert results["crash-call"]["error"]["code"] == "tool_execution_error"
    assert results["invalid-call"]["error"]["code"] == "invalid_tool_result"
    kept = results["failed-call"]["error"]["message"]
    assert _payload_rows(runtime) == [("failed-call", kept, "owner", '{"kept":true}')]


@pytest.mark.asyncio
async def test_a_cancelled_call_leaves_no_payload(tmp_path) -> None:
    attached = asyncio.Event()

    async def wait(context: ToolContext, _arguments: Any) -> Any:
        context.attach_result_payload({"orphan": True})
        attached.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    runtime = _runtime(tmp_path, _extension_tools(wait=wait), [_call("wait")])

    run = await build_chat_loop(runtime).start_run("coder", "wait", session_id="session-one")
    await attached.wait()
    run.request_cancel(reason="user")
    with pytest.raises(RunCancelledError):
        await run.wait()

    assert _payload_rows(runtime) == []


@pytest.mark.asyncio
async def test_only_extension_tools_can_attach_payloads(tmp_path) -> None:
    async def core_tool(context: ToolContext, _arguments: Any) -> Any:
        context.attach_result_payload({"core": True})
        return tool_success({})

    tools = ToolRegistry()
    tools.register("core_tool", "test-sentinel", _PARAMETERS, core_tool)
    runtime = _runtime(tmp_path, tools, [_call("core_tool"), {"content": "ok"}])

    run = await build_chat_loop(runtime).start_run("coder", "core", session_id="session-one")
    await run.wait()

    error = _tool_results(runtime)["call-one"]["error"]
    assert error["code"] == "tool_execution_error"
    assert error["message"] == "Result payloads are available only to Extension Tools"
    assert _payload_rows(runtime) == []


def test_calls_outside_a_session_cannot_attach_payloads(tmp_path) -> None:
    context = ToolContext(
        agent_id="coder",
        session_id="direct",
        run_id="run",
        tool_call_id="call",
        tool_name="save",
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
    )

    assert not context.result_payloads_available
    with pytest.raises(RuntimeError, match="unavailable for this Tool call"):
        context.attach_result_payload({"orphan": True})
