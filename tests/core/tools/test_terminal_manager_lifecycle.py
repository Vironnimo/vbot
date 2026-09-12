"""Terminal manager: lifecycle behavior."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import core.tools.terminal_backend as terminal_backend
import core.tools.terminal_manager as terminal_module
from core.tools.terminal_manager import (
    TerminalCapacityError,
    TerminalManager,
    TerminalNotOwnedError,
    TerminalOwner,
)
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    FakeTerminalAdapter,
    eventually,
    owner,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import (
    terminal_manager as terminal_manager,
)


@pytest.mark.asyncio
async def test_attachment_isolated_access_transfers_without_rewriting_origin(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    session = await spawn(manager, tmp_path)
    other = owner("session-b")

    with pytest.raises(TerminalNotOwnedError):
        manager.get_session(session.terminal_id, other)

    assert manager.transfer_scope(owner(), other) == 1
    assert manager.get_session(session.terminal_id, other) is session
    assert manager.list_sessions() == [session]
    assert session.owner == owner()
    assert session.lifecycle_owner == other
    assert session.attachment == other


@pytest.mark.asyncio
async def test_live_terminal_capacity_is_owner_scoped_and_globally_bounded(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _factory = terminal_manager
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_PER_SESSION", 2)
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_GLOBAL", 3)

    await spawn(manager, tmp_path, command="owner-a-1")
    await spawn(manager, tmp_path, command="owner-a-2")
    with pytest.raises(TerminalCapacityError):
        await spawn(manager, tmp_path, command="owner-a-3")

    other_owner = TerminalOwner("project-a", "agent-a", "session-b")
    await manager.spawn(
        other_owner,
        ["owner-b-1"],
        cwd=tmp_path,
        env=None,
        columns=120,
        rows=32,
        origin_run_id="run-b",
    )

    with pytest.raises(TerminalCapacityError, match="3"):
        await manager.spawn_for_operator(
            command="manual-terminal",
            arguments=[],
            cwd=tmp_path,
        )


@pytest.mark.asyncio
async def test_waiting_reader_does_not_block_input_resize_or_stop(tmp_path, monkeypatch) -> None:
    reading = threading.Event()

    class WaitingAdapter(FakeTerminalAdapter):
        def read(self, size: int) -> str:
            reading.set()
            return super().read(size)

    adapter = WaitingAdapter()
    manager = TerminalManager(adapter_factory=lambda *args: adapter)
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(loop, "_default_executor", executor)
        monkeypatch.setattr(
            terminal_backend, "terminate_process_tree", lambda child, **_kwargs: child.terminate()
        )
        try:
            session = await spawn(manager, tmp_path)
            await eventually(reading.is_set)
            await asyncio.wait_for(manager.send_operator_input(session.terminal_id, "hello"), 1)
            await asyncio.wait_for(
                manager.resize_for_operator(session.terminal_id, columns=90, rows=24), 1
            )
            await asyncio.wait_for(manager.kill_for_operator(session.terminal_id), 1)
            assert adapter.writes == ["hello"]
            assert adapter.resizes == [(24, 90)]
            assert not adapter.alive
        finally:
            adapter.finish()
            await manager.aclose()


@pytest.mark.asyncio
async def test_cancelled_start_waits_for_child_cleanup(tmp_path, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    adapter = FakeTerminalAdapter()

    def factory(*args):
        entered.set()
        assert release.wait(5)
        return adapter

    monkeypatch.setattr(
        terminal_backend, "terminate_process_tree", lambda child, **_kwargs: child.terminate()
    )
    manager = TerminalManager(adapter_factory=factory)
    task = asyncio.create_task(spawn(manager, tmp_path))
    try:
        await eventually(entered.is_set)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not adapter.alive
        assert not manager._pending_spawns
        assert all(session.state == "exited" for session in manager.list_sessions())
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_pending_starts_reserve_owner_and_global_capacity(tmp_path, monkeypatch) -> None:
    release = threading.Event()
    adapters: list[FakeTerminalAdapter] = []

    def factory(*args):
        adapter = FakeTerminalAdapter()
        adapters.append(adapter)
        assert release.wait(5)
        return adapter

    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_PER_SESSION", 2)
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_GLOBAL", 3)
    monkeypatch.setattr(
        terminal_backend, "terminate_process_tree", lambda child, **_kwargs: child.terminate()
    )
    manager = TerminalManager(adapter_factory=factory)
    tasks = [asyncio.create_task(spawn(manager, tmp_path)) for _ in range(2)]
    try:
        await eventually(lambda: len(adapters) == 2)
        with pytest.raises(TerminalCapacityError):
            await asyncio.wait_for(spawn(manager, tmp_path), 1)
        tasks.append(
            asyncio.create_task(
                manager.spawn(
                    owner("other"), ["fake"], cwd=tmp_path, env=None, origin_run_id="run-other"
                )
            )
        )
        await eventually(lambda: len(adapters) == 3)
        with pytest.raises(TerminalCapacityError):
            await asyncio.wait_for(
                manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path), 1
            )
        release.set()
        await asyncio.gather(*tasks)
        assert len(manager.list_sessions()) == 3
        assert not manager._pending_spawns
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_shutdown_waits_for_pending_start_and_closes_its_child(tmp_path, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    adapter = FakeTerminalAdapter()

    def factory(*args):
        entered.set()
        assert release.wait(5)
        return adapter

    monkeypatch.setattr(
        terminal_backend, "terminate_process_tree", lambda child, **_kwargs: child.terminate()
    )
    manager = TerminalManager(adapter_factory=factory)
    task = asyncio.create_task(spawn(manager, tmp_path))
    try:
        await eventually(entered.is_set)
        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(closing, 1)
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], terminal_module.TerminalLaunchError)
        assert not adapter.alive
        assert not manager.list_sessions()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_failed_start_releases_its_capacity_reservation(tmp_path, monkeypatch) -> None:
    attempts = 0
    adapter = FakeTerminalAdapter()

    def factory(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("test-owned launch failure")
        return adapter

    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_GLOBAL", 1)
    monkeypatch.setattr(
        terminal_backend, "terminate_process_tree", lambda child, **_kwargs: child.terminate()
    )
    manager = TerminalManager(adapter_factory=factory)
    try:
        with pytest.raises(terminal_module.TerminalLaunchError):
            await spawn(manager, tmp_path)
        assert not manager._pending_spawns
        await spawn(manager, tmp_path)
        assert len(manager.list_sessions()) == 1
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_parallel_terminal_ids_skip_collisions(terminal_manager, tmp_path, monkeypatch):
    from core.utils import ids

    manager, _ = terminal_manager
    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    first, second = await asyncio.gather(spawn(manager, tmp_path), spawn(manager, tmp_path))
    assert {first.terminal_id, second.terminal_id} == {"term_000000000001", "term_000000000002"}
    assert manager.get_session(first.terminal_id, owner()) is first
    assert manager.get_session(second.terminal_id, owner()) is second
