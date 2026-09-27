"""Per-Tool-Call cancellation wiring through tool dispatch."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat.messages import JsonObject
from core.runs import RunStatus
from core.tools import ToolContext, ToolRegistry, tool_failure, tool_success
from tests.core.chat.tool_dispatch_test_support import ToolDispatchHarness, call

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

CANCELLED = tool_failure("cancelled_by_user", "Command aborted by the user")
# Safety net for event waits; a passing test never waits this long.
_WAIT_SECONDS = 10.0


def _harness(tmp_path: Path, handler: Any) -> ToolDispatchHarness:
    tools = ToolRegistry()
    tools.register("cancellable", "Cancellable test Tool.", {"type": "object"}, handler)
    return ToolDispatchHarness(tmp_path, tools)


@pytest.mark.asyncio
async def test_each_call_registers_its_own_cancel_entry_which_dispatch_clears(
    tmp_path: Path,
) -> None:
    registered: list[str] = []

    def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        context.on_cancel(lambda: None)
        registered.append(context.tool_call_id)
        return tool_success({"tool_call_id": context.tool_call_id})

    harness = _harness(tmp_path, handler)

    await harness.dispatch([call("cancellable", "call-1"), call("cancellable", "call-2")])

    assert sorted(registered) == ["call-1", "call-2"]
    # Dispatch cleared both entries: nothing is left to cancel, and an id can be reused.
    assert harness.run.tool_call_cancelled("call-1") is False
    assert harness.run.cancel_tool_call("call-1") is False
    reused = await harness.dispatch([call("cancellable", "call-1")])
    assert reused.results == [tool_success({"tool_call_id": "call-1"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("register_late", [False, True], ids=["registered", "registers-late"])
async def test_per_call_cancel_yields_cancelled_envelope_and_leaves_the_run_running(
    tmp_path: Path, register_late: bool
) -> None:
    handler_entered = asyncio.Event()
    allow_registration = asyncio.Event()
    registered = asyncio.Event()
    cancel_fired = asyncio.Event()

    async def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        handler_entered.set()
        if register_late:
            # An accessor may click Cancel as soon as tool_call_started renders,
            # before the Tool registered its cleanup.
            await allow_registration.wait()
        context.on_cancel(cancel_fired.set)
        registered.set()
        await asyncio.wait_for(cancel_fired.wait(), timeout=_WAIT_SECONDS)
        return CANCELLED if context.was_cancelled_by_user() else tool_success({})

    harness = _harness(tmp_path, handler)
    dispatch = asyncio.create_task(harness.dispatch([call("cancellable", "call-cancel")]))
    await asyncio.wait_for(
        (handler_entered if register_late else registered).wait(), timeout=_WAIT_SECONDS
    )

    assert harness.run.cancel_tool_call("call-cancel") is True
    allow_registration.set()
    dispatched = await dispatch

    assert dispatched.results == [CANCELLED]
    assert dispatched.messages[0].tool_call_id == "call-cancel"
    assert (harness.run.cancel_requested, harness.run.status, harness.run.cancel_reason) == (
        False,
        RunStatus.RUNNING,
        None,
    )


@pytest.mark.asyncio
async def test_dispatch_returns_the_computed_result_when_a_run_cancel_arrives(
    tmp_path: Path,
) -> None:
    # The chat loop honors a Run cancel at its persist boundary; dispatch must
    # not silently drop a result the Tool already computed.
    started = asyncio.Event()
    finish = asyncio.Event()

    async def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        started.set()
        await finish.wait()
        return tool_success({"ok": True})

    harness = _harness(tmp_path, handler)
    dispatch = asyncio.create_task(harness.dispatch([call("cancellable")]))
    await asyncio.wait_for(started.wait(), timeout=_WAIT_SECONDS)

    harness.run.cancel_requested = True
    finish.set()

    assert (await dispatch).results == [tool_success({"ok": True})]
