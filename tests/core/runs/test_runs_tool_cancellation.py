"""Per-Tool-call cancellation and Run controls."""

from __future__ import annotations

import asyncio
import functools

import pytest

from core.runs import ChatRunManager, Run, RunStatus
from tests.core.runs.runs_test_support import SESSION

pytestmark = pytest.mark.asyncio


def _run() -> Run:
    return Run(run_id="run-one", agent_id="coder", session_id="session-one")


async def test_run_controls_reject_stale_calls_and_coalesce_compaction() -> None:
    run = Run(run_id="run-controls", agent_id="coder", session_id="session-one")
    assert not run.request_compaction()
    run.set_compaction_state("idle")
    assert run.request_compaction()
    sequence = run.events[-1].sequence
    assert run.request_compaction()
    assert run.events[-1].sequence == sequence
    run.emit("compaction_started")
    assert run.request_compaction()
    assert run.compaction_state == "running"
    run.emit("compaction_aborted", {"reason": "failed"})
    assert run.compaction_state == "idle"

    calls = []

    def background():
        calls.append(True)
        return True

    run.begin_tool_call("call-one")
    run.register_tool_background("call-one", background)
    assert run.controls()["background_tool_call_ids"] == ["call-one"]
    assert run.background_tool_call("call-one")
    assert not run.background_tool_call("call-one")
    assert calls == [True]
    run.begin_tool_call("call-two")
    run.register_tool_background("call-two", background)
    run.clear_tool_cancel("call-two")
    assert not run.background_tool_call("call-two")
    run.request_cancel()
    assert not run.request_compaction()
    assert run.controls() == {"compaction": "unavailable", "background_tool_call_ids": []}


async def test_tool_call_cancel_is_scoped_to_one_registered_call() -> None:
    run = _run()
    invocations: list[str] = []
    for call_id in ("tool-1", "tool-2", "tool-cleared"):
        run.register_tool_cancel(call_id, functools.partial(invocations.append, call_id))
    run.clear_tool_cancel("tool-cleared")

    assert run.cancel_tool_call("tool-missing") is False
    assert run.cancel_tool_call("tool-cleared") is False
    assert run.cancel_tool_call("tool-1") is True

    assert invocations == ["tool-1"]
    assert [
        run.tool_call_cancelled(call_id) for call_id in ("tool-1", "tool-2", "tool-missing")
    ] == [
        True,
        False,
        False,
    ]
    assert run.cancel_requested is False
    assert run.status == RunStatus.RUNNING

    # Clearing a finished call forgets its cancellation; a later cancel finds nothing.
    run.clear_tool_cancel("tool-1")
    assert run.tool_call_cancelled("tool-1") is False
    assert run.cancel_tool_call("tool-1") is False
    assert run.cancel_tool_call("tool-2") is True
    assert invocations == ["tool-1", "tool-2"]


@pytest.mark.parametrize("run_cancel", [False, True], ids=["tool-cancel", "run-cancel"])
async def test_cancel_before_callback_registration_fires_the_callback_on_registration(
    run_cancel: bool,
) -> None:
    run = _run()
    invocations: list[str] = []

    if run_cancel:
        run.request_cancel(reason="user")
    else:
        # An accessor may cancel a started call before its Tool registers cleanup.
        run.begin_tool_call("tool-1")
        assert run.cancel_tool_call("tool-1") is True
    run.register_tool_cancel("tool-1", lambda: invocations.append("aborted"))

    assert invocations == ["aborted"]
    assert run.tool_call_cancelled("tool-1") is True
    assert run.cancel_requested is run_cancel


async def test_tool_call_cancel_leaves_run_callbacks_and_executor_alone() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()
    run_callback_invocations: list[str] = []
    tool_invocations: list[str] = []

    async def execute(run: Run) -> str:
        run.add_cancel_callback(lambda: run_callback_invocations.append("run-cancel"))
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(SESSION, execute)
    await asyncio.sleep(0)

    async def abort() -> None:
        tool_invocations.append("async-abort")

    run.register_tool_cancel("tool-1", abort)
    assert run.cancel_tool_call("tool-1") is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert tool_invocations == ["async-abort"]
    assert run_callback_invocations == []
    release.set()
    assert await run.wait() == "done"
    assert run.status == RunStatus.COMPLETED


async def test_run_cancel_cascades_active_tool_callbacks_in_registration_order() -> None:
    run = _run()
    invocations: list[str] = []

    run.register_tool_cancel("tool-1", lambda: invocations.append("tool-1"))
    run.register_tool_cancel("tool-2", lambda: invocations.append("tool-2"))

    run.request_cancel(reason="user")

    assert invocations == ["tool-1", "tool-2"]
    assert run.tool_call_cancelled("tool-1") is True
    assert run.tool_call_cancelled("tool-2") is True
    assert run.cancel_requested is True
    assert run.cancel_reason == "user"
