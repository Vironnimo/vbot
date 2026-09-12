"""Terminal manager: cleanup behavior."""

from __future__ import annotations

import asyncio
import sys

import pytest

import core.tools.terminal_backend as terminal_backend
from core.tools.terminal_manager import (
    TerminalManager,
    TerminalManagerError,
)
from tests.core.tools.terminal_manager_helpers import (
    eventually,
    owner,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import (
    terminal_manager as terminal_manager,
)


@pytest.mark.asyncio
async def test_kill_closes_reader_even_without_tree_eof(terminal_manager, tmp_path, monkeypatch):
    manager, factory = terminal_manager
    monkeypatch.setattr(terminal_backend, "terminate_process_tree", lambda adapter, **_kwargs: None)
    for _ in range(35):
        session = await manager.spawn(
            owner(), ["fake"], cwd=tmp_path, env=None, origin_run_id="run"
        )
        adapter = factory.adapters[-1]
        # The fake's read is still blocked, as when an escaped child holds
        # the PTY slave. Closing the transport must release it independently.
        await asyncio.wait_for(manager.kill_for_operator(session.terminal_id), 1)
        assert session.reader_task.done()
        adapter.alive = False
    assert (
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(manager._reader_executor, lambda: 42), 1
        )
        == 42
    )


@pytest.mark.asyncio
async def test_real_terminal_output_and_idle_reader_shutdown(tmp_path):
    manager = TerminalManager(activity_quiet_seconds=0.03)
    try:
        session = await manager.spawn(
            owner(),
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('real-terminal-ready', flush=True); time.sleep(30)",
            ],
            cwd=tmp_path,
            env=None,
            origin_run_id="run",
        )
        await eventually(
            lambda: "real-terminal-ready" in session.renderer.screen_text(), attempts=1000
        )
        await asyncio.wait_for(manager.kill_for_operator(session.terminal_id), 10)
        assert session.state == "exited"
        assert session.reader_task.done()
        assert not session.adapter.is_alive()
    finally:
        await asyncio.wait_for(manager.aclose(), 10)


@pytest.mark.asyncio
async def test_failed_tree_kill_retains_orphan_after_root_eof_for_retry(
    terminal_manager, tmp_path, monkeypatch
):
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    orphan = object()
    attempts = []

    def kill_tree(child, *, targets):
        assert child is adapter
        attempts.append(targets)
        if len(attempts) == 1:
            targets.append(orphan)
            adapter.finish(-1)
            raise PermissionError("descendant still running")
        assert targets == [orphan]
        assert not adapter.is_alive()

    monkeypatch.setattr("core.tools.terminal_backend.kill_process_tree", kill_tree)
    with pytest.raises(TerminalManagerError, match="Retry the kill operation"):
        await manager.kill(session.terminal_id, owner())
    await asyncio.wait_for(asyncio.shield(session.reader_task), 2)
    assert session.termination_pending
    assert session.state not in {"exited", "error"}
    assert session.finished_at is None

    await manager.kill(session.terminal_id, owner())
    assert attempts[0] is attempts[1]
    assert session.state == "exited"
    assert not session.termination_pending


@pytest.mark.asyncio
async def test_shutdown_attempts_other_terminals_and_retains_failed_tree_for_retry(
    terminal_manager, tmp_path, monkeypatch
):
    manager, factory = terminal_manager
    first = await spawn(manager, tmp_path)
    second = await spawn(manager, tmp_path)
    denied = True

    def kill_tree(child, *, targets):
        if child is factory.adapters[0] and denied:
            raise PermissionError("descendant still running")
        child.terminate()

    monkeypatch.setattr("core.tools.terminal_backend.kill_process_tree", kill_tree)
    sweeper = manager._sweeper_task
    with pytest.raises(TerminalManagerError, match=first.terminal_id):
        await manager.aclose()
    assert sweeper.done()
    assert second.reader_task.done()
    assert first.termination_pending
    assert factory.adapters[0].alive
    assert not factory.adapters[1].alive
    assert not second.termination_pending
    denied = False
    manager.stop()
    assert not factory.adapters[0].alive
    assert not first.termination_pending
