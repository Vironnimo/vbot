"""The fixed proxy preserves the target Tool's dispatch and Run contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

import core.tools.call_tool as proxy_tool
import core.tools.execute_code as execute_code
from core.chat._message_history import compaction_projection_without_active_skills
from core.chat._step_outcomes import _RepeatedToolCalls
from core.chat.messages import ToolCall
from core.chat.wire_shaping import _embed_notes_into_request
from core.sessions import ChatSessionManager
from core.tools import ToolContext, ToolRegistry, tool_failure, tool_success
from core.tools._code_process import CodeExecutionResult
from core.tools.call_tool import register_call_tool
from core.tools.load_tools import register_load_tools_tool
from resources.extensions.mcp.content import RESULT_VIEW_CHARACTERS, ContentStore
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop
from tests.core.chat.tool_dispatch_test_support import ToolDispatchHarness
from tests.core.permissions.command_check_support import FakeAnalyzer, fake_checker

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


def _proxy(name: str, arguments: dict[str, Any], call_id: str = "proxy") -> ToolCall:
    return ToolCall(id=call_id, name="call_tool", arguments={"name": name, "arguments": arguments})


@pytest.mark.parametrize("denial", [None, "restriction", "live", "removed", "arguments"])
async def test_proxy_preserves_target_validation_authority_and_result(
    tmp_path: Path, denial: str | None
) -> None:
    seen: list[dict[str, Any]] = []
    tools = ToolRegistry()
    register_call_tool(tools)

    def target(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        seen.append(arguments)
        assert context.parent_tool_name == "call_tool"
        assert not context.programmatic
        return tool_success({"value": arguments["value"]})

    tools.register(
        "target",
        "test sentinel",
        {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
        target,
        open_input_schema=True,
    )
    harness = ToolDispatchHarness(tmp_path, tools)
    result = await harness.dispatch(
        [_proxy("target", {} if denial == "arguments" else {"value": 7})],
        base_allowed_tools=["call_tool", "target"],
        tool_restriction=[] if denial == "restriction" else None,
        tool_denial_resolver=(lambda name: "test refusal" if name == "target" else None)
        if denial == "live"
        else None,
        removed_tool_names=frozenset({"target"}) if denial == "removed" else frozenset(),
    )
    [envelope] = result.results
    if denial is None:
        assert envelope == tool_success({"value": 7})
        assert seen == [{"value": 7}]
    else:
        assert not seen and not envelope["ok"]
        assert (
            envelope["error"]["code"]
            == {
                "restriction": "tool_not_allowed",
                "live": "tool_not_allowed",
                "removed": "tool_removed",
                "arguments": "invalid_arguments",
            }[denial]
        )


async def test_proxy_forwards_request_media_and_counts_the_target_for_repetition(
    tmp_path: Path,
) -> None:
    tools = ToolRegistry()
    register_call_tool(tools)
    tracker = _RepeatedToolCalls()
    observed: list[str] = []
    notes: list[str] = []

    def observe(name: str, arguments: dict[str, Any]) -> str | None:
        observed.append(name)
        note = tracker.observe(name, arguments)
        if note:
            notes.append(note)
        return note

    def target(context: ToolContext, _arguments: dict[str, Any]) -> dict[str, Any]:
        context.result_media.append({"media_type": "image/png", "data": "test-owned-data"})
        return tool_success({"value": 7})

    tools.register("target", "test sentinel", {"type": "object"}, target)
    harness = ToolDispatchHarness(tmp_path, tools)
    result = await harness.dispatch(
        [_proxy("target", {}, str(index)) for index in range(3)],
        base_allowed_tools=["call_tool", "target"],
        tool_repetition_observer=observe,
    )
    assert len(result.media_outputs) == 3
    assert {media["tool_call_id"] for media in result.media_outputs} == {"0", "1", "2"}
    assert result.media_outputs[0]["data"] == "test-owned-data"
    assert observed == ["target"] * 3
    assert len(notes) == 1


async def test_proxy_can_execute_python_and_python_still_cannot_recurse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = ToolRegistry()
    register_call_tool(tools)
    tools.register("bash", "test sentinel", {"type": "object"}, lambda _c, _a: tool_success({}))
    execute_code.register_execute_code_tool(
        tools, command_checker=fake_checker(FakeAnalyzer()), credential_names=lambda: set()
    )

    async def worker(_code: str, **kwargs: Any) -> CodeExecutionResult:
        result = await kwargs["invoke"]("execute_code", {"code": "print('recursive')"})
        assert result["error"]["code"] == "code_recursion"
        return CodeExecutionResult("completed", 0, "selected\n", "", 1, 1, [])

    monkeypatch.setattr(execute_code, "run_python", worker)
    harness = ToolDispatchHarness(tmp_path, tools)
    result = await harness.dispatch(
        [_proxy("execute_code", {"code": "test-owned program"})],
        base_allowed_tools=["call_tool", "execute_code", "bash"],
    )
    assert result.results[0]["data"]["status"] == "completed"
    assert result.results[0]["data"]["output"] == "selected\n"


async def test_proxy_refuses_recursion_before_the_target_runs(tmp_path: Path) -> None:
    tools = ToolRegistry()
    register_call_tool(tools)
    result = await ToolDispatchHarness(tmp_path, tools).dispatch(
        [_proxy("call_tool", {"name": "call_tool", "arguments": {}})],
        base_allowed_tools=["call_tool"],
    )
    assert result.results[0]["error"]["code"] == "proxy_recursion"


async def test_proxy_preserves_mcp_pagination_instead_of_python_raw_results(tmp_path: Path) -> None:
    tools = ToolRegistry()
    register_call_tool(tools)
    content = "test-owned-content " * RESULT_VIEW_CHARACTERS

    async def target(context: ToolContext, _arguments: dict[str, Any]) -> dict[str, Any]:
        view, artifacts = await ContentStore(cast(Any, None)).present(
            {"content": [{"type": "text", "text": content}]}, context, "example"
        )
        return tool_success(view, artifacts=artifacts)

    tools.register("mcp_example", "test sentinel", {"type": "object"}, target, extension="mcp")
    harness = ToolDispatchHarness(tmp_path, tools)
    result = await harness.dispatch(
        [_proxy("mcp_example", {})], base_allowed_tools=["call_tool", "mcp_example"]
    )
    data = result.results[0]["data"]
    assert data["result_id"] and len(json.dumps(data)) < RESULT_VIEW_CHARACTERS
    manager = ChatSessionManager(tmp_path)
    try:
        document = await manager.tool_result_payload_async(
            harness.session.address, data["result_id"], owner_name="mcp"
        )
    finally:
        manager.close()
    assert document is not None
    assert document["payload"]["content"][0]["text"] == content


async def test_proxy_skill_body_uses_the_expiring_context_note_once(tmp_path: Path) -> None:
    tools = ToolRegistry()
    register_call_tool(tools)
    body = "test-owned Skill instructions"

    def skill(context: ToolContext, _arguments: dict[str, Any]) -> dict[str, Any]:
        context.activate_skill("example", body, None)
        return tool_success({"status": "loaded", "name": "example", "content": body})

    tools.register("skill", "test sentinel", {"type": "object"}, skill)
    harness = ToolDispatchHarness(tmp_path, tools)
    result = await harness.dispatch(
        [_proxy("skill", {})], base_allowed_tools=["call_tool", "skill"]
    )
    messages = [*harness.session.load(), *result.messages]
    assert body not in json.dumps(result.results)
    assert str(_embed_notes_into_request(messages)).count(body) == 1
    compacted = compaction_projection_without_active_skills(messages)
    assert body not in str(_embed_notes_into_request(compacted))


@pytest.mark.parametrize(
    "late_registration", [False, True, None], ids=["running", "registering", "before-child"]
)
async def test_stopping_the_proxy_cancels_its_target_without_cancelling_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_registration: bool | None
) -> None:
    tools = ToolRegistry()
    original = proxy_tool._call_tool

    async def stop_before_child(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        active = runtime.chat_run_manager.active_run(
            agent_id="coder", session_id="session-one", project_id=None
        )
        assert active is not None and active.cancel_tool_call(context.tool_call_id)
        return await original(context, arguments)

    if late_registration is None:
        monkeypatch.setattr(proxy_tool, "_call_tool", stop_before_child)
    register_call_tool(tools)
    register_load_tools_tool(tools)
    entered, register, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def target(context: ToolContext, _arguments: dict[str, Any]) -> dict[str, Any]:
        if not late_registration:
            context.on_cancel(stopped.set)
        entered.set()
        if late_registration:
            await register.wait()
            context.on_cancel(stopped.set)
        await stopped.wait()
        return tool_failure("cancelled_by_user", "test-owned cancellation")

    tools.register("target", "test sentinel", {"type": "object"}, target)
    runtime = StubRuntime(
        data_dir=tmp_path,
        tools=tools,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["target"]),
        adapter=StubAdapter(
            [
                {
                    "tool_calls": [
                        {
                            "id": "proxy",
                            "name": "call_tool",
                            "arguments": {"name": "target", "arguments": {}},
                        }
                    ]
                },
                {"content": "Finished"},
            ],
            on_demand_tools=False,
        ),
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    run = await build_chat_loop(runtime).start_run("coder", "go", session_id="session-one")
    if late_registration is None:
        await run.wait()
        assert not entered.is_set() and not run.cancel_requested
        return
    await entered.wait()
    assert run.cancel_tool_call("proxy")
    register.set()
    await run.wait()
    assert stopped.is_set() and not run.cancel_requested


async def test_stopping_proxied_python_keeps_the_conversation_run_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = ToolRegistry()
    register_call_tool(tools)
    register_load_tools_tool(tools)
    tools.register("bash", "test sentinel", {"type": "object"}, lambda _c, _a: tool_success({}))
    execute_code.register_execute_code_tool(
        tools, command_checker=fake_checker(FakeAnalyzer()), credential_names=lambda: set()
    )
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def worker(_code: str, **kwargs: Any) -> CodeExecutionResult:
        entered.set()
        try:
            await kwargs["stop"].wait()
            return CodeExecutionResult("stopped", None, "", "", 0, 0, [])
        finally:
            stopped.set()

    monkeypatch.setattr(execute_code, "run_python", worker)
    runtime = StubRuntime(
        data_dir=tmp_path,
        tools=tools,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["execute_code", "bash"]),
        adapter=StubAdapter(
            [
                {
                    "tool_calls": [
                        {
                            "id": "proxy",
                            "name": "call_tool",
                            "arguments": {
                                "name": "execute_code",
                                "arguments": {"code": "test-owned program"},
                            },
                        }
                    ]
                },
                {"content": "Finished"},
            ],
            on_demand_tools=False,
        ),
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    run = await build_chat_loop(runtime).start_run("coder", "go", session_id="session-one")
    await entered.wait()
    assert run.cancel_tool_call("proxy")
    await run.wait()
    assert stopped.is_set() and not run.cancel_requested
