"""Extension tool_call and tool_result hooks, runtime denials and recovered calls in dispatch."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, override

import pytest

from core.chat.messages import JsonObject, ToolCall, ToolCallRejection
from core.extensions import Deny, ExtensionRegistry, HookContext, Modify, Replace
from core.runs import TOOL_CALL_RESULT_EVENT, TOOL_CALL_STARTED_EVENT
from core.tools import ToolContext, ToolRegistry, tool_failure, tool_success
from tests.core.chat.tool_dispatch_test_support import ToolDispatchHarness, call

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

# Safety net for event waits; a passing test never waits this long.
_WAIT_SECONDS = 10.0


def _recording_tool(executed: list[str], *, parameters: JsonObject | None = None) -> ToolRegistry:
    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        executed.append(context.tool_call_id)
        return tool_success({"ran": context.tool_call_id, "arguments": arguments})

    tools = ToolRegistry()
    tools.register("echo", "Echo input.", parameters or {"type": "object"}, handler)
    return tools


def _extensions(event: str, handler: Any, extension: str = "observer") -> ExtensionRegistry:
    registry = ExtensionRegistry()
    registry.install_handler(extension, event, handler)
    return registry


class _FailingHookDispatch(ExtensionRegistry):
    """Hook dispatch that raises outside the isolation of any single handler."""

    def __init__(self, failing_hook: str) -> None:
        super().__init__()
        self._failing_hook = failing_hook

    @override
    async def dispatch_tool_call(self, ctx: HookContext, **payload: Any) -> Any:
        if self._failing_hook == "tool_call":
            raise RuntimeError("hook dispatch broke")
        return await super().dispatch_tool_call(ctx, **payload)

    @override
    async def dispatch_tool_result(self, ctx: HookContext, **payload: Any) -> Any:
        if self._failing_hook == "tool_result":
            raise RuntimeError("hook dispatch broke")
        return await super().dispatch_tool_result(ctx, **payload)


def _sequence_call(call_id: str, index: int, **arguments: Any) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="echo",
        arguments=arguments,
        argument_sequence_index=index,
        argument_sequence_length=2,
    )


@pytest.mark.asyncio
async def test_rejected_call_bypasses_hooks_and_handler(tmp_path: Path) -> None:
    executed: list[str] = []
    hook_calls: list[str] = []
    registry = _extensions("tool_call", lambda _c, **_p: hook_calls.append("tool_call"))
    registry.install_handler(
        "observer", "tool_result", lambda _c, **_p: hook_calls.append("tool_result")
    )
    harness = ToolDispatchHarness(tmp_path, _recording_tool(executed), extensions=registry)
    rejection = ToolCallRejection(
        code="malformed_tool_arguments", message="Arguments were malformed.", fingerprint="sha256"
    )

    dispatched = await harness.dispatch(
        [ToolCall(id="call-bad", name="echo", arguments={}, rejection=rejection)]
    )

    assert (executed, hook_calls) == ([], [])
    assert dispatched.results == [
        tool_failure("malformed_tool_arguments", "Arguments were malformed.", retryable=False)
    ]


@pytest.mark.asyncio
async def test_invalid_recovered_call_does_not_block_valid_sequence_sibling(
    tmp_path: Path,
) -> None:
    executed: list[str] = []
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    harness = ToolDispatchHarness(tmp_path, _recording_tool(executed, parameters=schema))

    dispatched = await harness.dispatch(
        [_sequence_call("call-invalid", 0), _sequence_call("call-valid", 1, value="ok")]
    )

    assert dispatched.results[0]["error"]["code"] == "invalid_arguments"
    assert dispatched.results[1] == tool_success(
        {"ran": "call-valid", "arguments": {"value": "ok"}}
    )
    assert executed == ["call-valid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_hooks", [False, True], ids=["recovered-sequence", "with-hooks"])
async def test_sibling_handlers_run_in_parallel_while_hooks_serialize(
    tmp_path: Path, with_hooks: bool
) -> None:
    active_tools = max_active_tools = active_hooks = max_active_hooks = 0
    both_tools_started = asyncio.Event()

    async def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        nonlocal active_tools, max_active_tools
        active_tools += 1
        max_active_tools = max(max_active_tools, active_tools)
        if active_tools == 2:
            both_tools_started.set()
        try:
            await asyncio.wait_for(both_tools_started.wait(), timeout=_WAIT_SECONDS)
            return tool_success({"ran": context.tool_call_id})
        finally:
            active_tools -= 1

    async def hook(_context: HookContext, **_payload: Any) -> None:
        nonlocal active_hooks, max_active_hooks
        active_hooks += 1
        max_active_hooks = max(max_active_hooks, active_hooks)
        await asyncio.sleep(0.01)
        active_hooks -= 1

    tools = ToolRegistry()
    tools.register(
        "echo",
        "Echo input.",
        {"type": "object", "additionalProperties": False},
        handler,
    )
    registry = None
    if with_hooks:
        registry = _extensions("tool_call", hook)
        registry.install_handler("observer", "tool_result", hook)
    harness = ToolDispatchHarness(tmp_path, tools, extensions=registry)
    calls = (
        [call("echo", "call-first"), call("echo", "call-second")]
        if with_hooks
        else [_sequence_call("call-first", 0), _sequence_call("call-second", 1)]
    )

    dispatched = await harness.dispatch(calls)

    assert max_active_tools == 2
    assert max_active_hooks == (1 if with_hooks else 0)
    assert dispatched.results == [
        tool_success({"ran": "call-first"}),
        tool_success({"ran": "call-second"}),
    ]


@pytest.mark.asyncio
async def test_runtime_denial_precedes_handlers_and_extension_hooks(tmp_path: Path) -> None:
    executed: list[str] = []
    hook_calls: list[str] = []
    denial = (
        "Tool access denied: the current sender is a group member. "
        "Group members may use only web_search and web_fetch."
    )
    registry = _extensions("tool_call", lambda _c, **_p: hook_calls.append("called"))
    registry.install_handler("observer", "tool_result", lambda _c, **_p: hook_calls.append("x"))
    harness = ToolDispatchHarness(tmp_path, _recording_tool(executed), extensions=registry)

    dispatched = await harness.dispatch(
        [call("echo", "call-1"), call("echo", "call-2")],
        tool_denial_resolver=lambda _tool_name: denial,
    )

    assert (executed, hook_calls) == ([], [])
    denied = {
        "ok": False,
        "error": {"code": "tool_not_allowed", "message": denial},
        "data": None,
        "artifacts": [],
    }
    assert dispatched.results == [denied, denied]


@pytest.mark.asyncio
async def test_denied_tool_call_yields_error_envelope_and_never_executes(tmp_path: Path) -> None:
    executed: list[str] = []
    registry = _extensions("tool_call", lambda _c, **_p: Deny("not allowed here"), "guard")
    harness = ToolDispatchHarness(tmp_path, _recording_tool(executed), extensions=registry)

    dispatched = await harness.dispatch([call("echo", x=1)])

    [result] = dispatched.results
    assert result["error"]["code"] == "tool_call_denied"
    assert "not allowed here" in result["error"]["message"]
    assert "guard" in result["error"]["message"]
    assert executed == []


@pytest.mark.asyncio
async def test_modified_input_reaches_handler_and_started_event(tmp_path: Path) -> None:
    executed: list[str] = []
    registry = _extensions("tool_call", lambda _c, **_p: Modify({"cmd": "rewritten"}), "rewriter")
    harness = ToolDispatchHarness(tmp_path, _recording_tool(executed), extensions=registry)

    dispatched = await harness.dispatch([call("echo", cmd="original")])

    assert dispatched.results[0]["data"]["arguments"] == {"cmd": "rewritten"}
    [started] = dispatched.events(TOOL_CALL_STARTED_EVENT)
    assert started.payload["tool_call"]["arguments"] == {"cmd": "rewritten"}


@pytest.mark.asyncio
async def test_replace_short_circuits_with_envelope(tmp_path: Path) -> None:
    executed: list[str] = []
    replacement = tool_success({"replaced": True})
    registry = _extensions("tool_call", lambda _c, **_p: Replace(replacement), "replacer")
    harness = ToolDispatchHarness(tmp_path, _recording_tool(executed), extensions=registry)

    dispatched = await harness.dispatch([call("echo")])

    assert dispatched.results == [replacement]
    assert executed == []


@pytest.mark.asyncio
async def test_tool_result_hook_replaces_envelope(tmp_path: Path) -> None:
    replacement = tool_success({"patched": True})
    registry = _extensions("tool_result", lambda _c, **_p: replacement, "patcher")
    harness = ToolDispatchHarness(tmp_path, _recording_tool([]), extensions=registry)

    assert (await harness.dispatch([call("echo")])).results == [replacement]


@pytest.mark.asyncio
async def test_add_note_from_hook_lands_in_session(tmp_path: Path) -> None:
    def note_hook(context: Any, **_payload: Any) -> None:
        context.add_note("hook was here")

    registry = _extensions("tool_call", note_hook, "noter")
    harness = ToolDispatchHarness(tmp_path, _recording_tool([]), extensions=registry)

    await harness.dispatch([call("echo")])

    assert "hook was here" in [m.content for m in harness.session.load() if m.role == "note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_hook", ["tool_call", "tool_result"])
async def test_failing_hook_dispatch_still_yields_one_result(
    tmp_path: Path, failing_hook: str
) -> None:
    executed: list[str] = []
    harness = ToolDispatchHarness(
        tmp_path, _recording_tool(executed), extensions=_FailingHookDispatch(failing_hook)
    )

    dispatched = await harness.dispatch([call("echo", "call-1")])

    [result] = dispatched.results
    assert result["error"]["code"] == "tool_execution_error"
    assert "hook dispatch broke" in result["error"]["message"]
    assert len(dispatched.events(TOOL_CALL_STARTED_EVENT)) == 1
    [finished] = dispatched.events(TOOL_CALL_RESULT_EVENT)
    assert finished.payload["result"] == result
    assert executed == ([] if failing_hook == "tool_call" else ["call-1"])
