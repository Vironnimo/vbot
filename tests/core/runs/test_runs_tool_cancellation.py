"""Per-tool-call cancellation tests."""

from __future__ import annotations

from core.sessions import SessionAddress

from .runs_test_support import (
    ChatRunManager,
    Run,
    RunStatus,
    asyncio,
    pytest,
)

pytestmark = pytest.mark.asyncio


async def test_cancel_tool_call_fires_callback_and_flips_state_without_cancelling_run() -> None:
    """cancel_tool_call must fire the callback, mark cancelled, and leave the run alive."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []

    def abort() -> None:
        invocations.append("aborted")

    run.register_tool_cancel("tool-1", abort)

    cancelled = run.cancel_tool_call("tool-1")

    assert cancelled is True
    assert invocations == ["aborted"]
    assert run.tool_call_cancelled("tool-1") is True
    assert run.cancel_requested is False
    assert run.status == RunStatus.RUNNING


async def test_cancel_tool_call_with_unknown_id_returns_false() -> None:
    """cancel_tool_call must be a no-op for an id that was never registered."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []

    def abort() -> None:
        invocations.append("aborted")

    run.register_tool_cancel("tool-1", abort)

    cancelled = run.cancel_tool_call("tool-missing")

    assert cancelled is False
    assert invocations == []
    assert run.tool_call_cancelled("tool-missing") is False
    assert run.tool_call_cancelled("tool-1") is False
    assert run.cancel_requested is False


async def test_cancel_started_tool_call_before_callback_registration_fires_callback_later() -> None:
    """An immediate accessor cancel must survive until Tool cleanup registers."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []
    run.begin_tool_call("tool-1")

    cancelled = run.cancel_tool_call("tool-1")
    run.register_tool_cancel("tool-1", lambda: invocations.append("aborted"))

    assert cancelled is True
    assert invocations == ["aborted"]
    assert run.tool_call_cancelled("tool-1") is True
    assert run.cancel_requested is False


async def test_cancel_tool_call_after_clear_returns_false() -> None:
    """clear_tool_cancel drops the entry; subsequent cancel_tool_call returns False."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []

    def abort() -> None:
        invocations.append("aborted")

    run.register_tool_cancel("tool-1", abort)
    run.clear_tool_cancel("tool-1")

    cancelled = run.cancel_tool_call("tool-1")

    assert cancelled is False
    assert invocations == []
    assert run.tool_call_cancelled("tool-1") is False
    assert run.cancel_requested is False


async def test_clear_tool_cancel_resets_cancelled_state() -> None:
    """clear_tool_cancel removes a cancelled entry so tool_call_cancelled returns False."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    def abort() -> None:
        return None

    run.register_tool_cancel("tool-1", abort)
    assert run.cancel_tool_call("tool-1") is True
    assert run.tool_call_cancelled("tool-1") is True

    run.clear_tool_cancel("tool-1")

    assert run.tool_call_cancelled("tool-1") is False
    assert run.cancel_tool_call("tool-1") is False


async def test_tool_call_cancel_does_not_invoke_run_cancel_callbacks_or_cancel_task() -> None:
    """Per-tool-call cancel must not touch run-level cancel callbacks or the executor task."""
    manager = ChatRunManager()
    release = asyncio.Event()
    run_callback_invocations: list[str] = []
    tool_invocations: list[str] = []

    async def execute(run: Run) -> str:
        run.add_cancel_callback(lambda: run_callback_invocations.append("run-cancel"))
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    await asyncio.sleep(0)
    run.register_tool_cancel("tool-1", lambda: tool_invocations.append("tool-abort"))

    cancelled = run.cancel_tool_call("tool-1")
    assert cancelled is True
    assert tool_invocations == ["tool-abort"]
    assert run_callback_invocations == []
    assert run.cancel_requested is False
    assert run._task is not None and not run._task.done()  # noqa: SLF001 - task must stay alive.

    release.set()
    assert await run.wait() == "done"
    assert run.status == RunStatus.COMPLETED


async def test_tool_call_cancel_supports_async_callback() -> None:
    """Per-tool-call cancel can dispatch async callbacks via the existing scheduler."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []

    async def abort() -> None:
        invocations.append("async-abort")

    run.register_tool_cancel("tool-1", abort)

    assert run.cancel_tool_call("tool-1") is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert invocations == ["async-abort"]


async def test_tool_call_cancel_isolates_state_between_tool_call_ids() -> None:
    """Cancelling one tool call must not flip tool_call_cancelled for a different id."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    def abort() -> None:
        return None

    run.register_tool_cancel("tool-1", abort)
    run.register_tool_cancel("tool-2", abort)

    assert run.cancel_tool_call("tool-1") is True

    assert run.tool_call_cancelled("tool-1") is True
    assert run.tool_call_cancelled("tool-2") is False
    assert run.cancel_tool_call("tool-2") is True
    assert run.tool_call_cancelled("tool-2") is True


async def test_run_cancel_cascades_active_tool_callbacks_in_registration_order() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []

    run.register_tool_cancel("tool-1", lambda: invocations.append("tool-1"))
    run.register_tool_cancel("tool-2", lambda: invocations.append("tool-2"))

    run.request_cancel(reason="user")

    assert invocations == ["tool-1", "tool-2"]
    assert run.tool_call_cancelled("tool-1") is True
    assert run.tool_call_cancelled("tool-2") is True
    assert run.cancel_requested is True
    assert run.cancel_reason == "user"


async def test_tool_cancel_registered_after_run_cancel_fires_immediately() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []
    run.request_cancel(reason="user")

    run.register_tool_cancel("tool-late", lambda: invocations.append("tool-late"))

    assert invocations == ["tool-late"]
    assert run.tool_call_cancelled("tool-late") is True
