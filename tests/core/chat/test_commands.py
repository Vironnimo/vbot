"""Tests for slash command dispatch."""

from __future__ import annotations

import asyncio

import pytest

from core.chat import (
    ChatRunManager,
    CommandDispatcher,
    CommandHandled,
    NotACommand,
    Run,
    RunCancelledError,
)


@pytest.mark.asyncio
async def test_dispatch_stop_with_active_run_returns_cancelled_reply() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        started.set()
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await started.wait()

    dispatcher = CommandDispatcher(manager)
    result = dispatcher.dispatch("coder", "session-one", " /STOP ")

    assert isinstance(result, CommandHandled)
    assert result.reply == "Run cancelled."
    assert run.cancel_requested is True

    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()


def test_dispatch_stop_with_no_active_run_returns_not_found_reply() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/stop")

    assert isinstance(result, CommandHandled)
    assert result.reply == "No active run to cancel."


def test_dispatch_unknown_command_returns_not_a_command() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/bogus")

    assert isinstance(result, NotACommand)


def test_dispatch_non_command_message_returns_not_a_command() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "hello")

    assert isinstance(result, NotACommand)
