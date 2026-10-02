"""Terminal manager: attachment scopes, capacity, launches in flight, kill and shutdown."""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import override

import pytest

import core.tools.terminal_backend as terminal_backend
import core.tools.terminal_manager as terminal_module
from core.runs import RunExecutionOwner
from core.tools.terminal_manager import (
    TerminalAlreadyAttachedError,
    TerminalCapacityError,
    TerminalClosedError,
    TerminalManager,
    TerminalManagerError,
    TerminalNotOwnedError,
    TerminalOwner,
)
from tests.core.tools.terminal_manager_helpers import (
    TEST_ACTIVITY_QUIET_SECONDS,
    AdapterFactory,
    FakeClock,
    FakeTerminalAdapter,
    PendingTriggerService,
    establish_delivered_baseline,
    eventually,
    owner,
    session_of,
    settle_next_activity,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import clocked_manager as clocked_manager
from tests.core.tools.terminal_manager_helpers import shell_environment as shell_environment
from tests.core.tools.terminal_manager_helpers import terminal_manager as terminal_manager


@pytest.fixture
def terminate_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a fake adapter directly instead of signalling a process tree."""
    monkeypatch.setattr(
        terminal_backend, "terminate_process_tree", lambda child, **_kwargs: child.terminate()
    )


@pytest.mark.asyncio
async def test_session_move_transfers_attachment_and_only_an_agent_terminals_lifecycle(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    agent_started = await spawn(manager, tmp_path)
    manual = await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
    operator_started = session_of(manager, manual["terminal_id"])
    manager.attach(operator_started.terminal_id, owner(), origin_run_id="run-a")
    target = owner("session-b")
    with pytest.raises(TerminalNotOwnedError):
        manager.get_session(agent_started.terminal_id, target)

    assert manager.transfer_scope(owner(), target) == 2

    for session in (agent_started, operator_started):
        assert manager.get_session(session.terminal_id, target) is session
        assert session.attachment == target
    assert (agent_started.owner, agent_started.lifecycle_owner) == (owner(), target)
    assert (operator_started.owner, operator_started.lifecycle_owner) == (None, None)
    assert len(manager.list_sessions()) == 2

    await manager.close_scope(target)
    assert operator_started.attachment is None
    assert factory.adapters[1].alive is True
    assert factory.adapters[0].alive is False


@pytest.mark.asyncio
async def test_attach_rejects_another_session_and_detached_agent_origin_still_owns_lifecycle(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    other = owner("session-b")

    with pytest.raises(TerminalAlreadyAttachedError):
        manager.attach(session.terminal_id, other, origin_run_id="run-b")

    manager.detach(session.terminal_id, owner())
    attached, changed = manager.attach(session.terminal_id, other, origin_run_id="run-b")
    assert attached is session
    assert changed is True
    assert (session.owner, session.lifecycle_owner, session.attachment) == (
        owner(),
        owner(),
        other,
    )

    await manager.close_scope(other)
    assert factory.adapters[0].alive is True
    assert session.attachment is None

    await manager.close_scope(owner())
    assert factory.adapters[0].alive is False


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
    await manager.spawn(other_owner, ["owner-b-1"], cwd=tmp_path, env=None, origin_run_id="run-b")

    with pytest.raises(TerminalCapacityError, match="3"):
        await manager.spawn_for_operator(command="manual-terminal", arguments=[], cwd=tmp_path)


@pytest.mark.asyncio
@pytest.mark.usefixtures("terminate_directly")
async def test_pending_starts_reserve_owner_and_global_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    adapters: list[FakeTerminalAdapter] = []

    def factory(*args):  # type: ignore[no-untyped-def]
        adapter = FakeTerminalAdapter()
        adapters.append(adapter)
        assert release.wait(5)
        return adapter

    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_PER_SESSION", 2)
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_GLOBAL", 3)
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
@pytest.mark.usefixtures("terminate_directly")
async def test_waiting_reader_does_not_block_input_resize_or_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reading = threading.Event()

    class WaitingAdapter(FakeTerminalAdapter):
        @override
        def read(self, size: int) -> str:
            reading.set()
            return super().read(size)

    adapter = WaitingAdapter()
    manager = TerminalManager(adapter_factory=lambda *args: adapter)
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        # A one-thread default executor: a blocked read there would stall every other call.
        monkeypatch.setattr(loop, "_default_executor", executor)
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
@pytest.mark.usefixtures("terminate_directly")
async def test_cancelled_start_waits_for_child_cleanup(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    adapter = FakeTerminalAdapter()

    def factory(*args):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(5)
        return adapter

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
@pytest.mark.usefixtures("terminate_directly")
async def test_shutdown_waits_for_pending_start_and_closes_its_child(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    adapter = FakeTerminalAdapter()

    def factory(*args):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(5)
        return adapter

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
async def test_parallel_terminal_ids_skip_collisions(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.utils import ids

    manager, _ = terminal_manager
    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    first, second = await asyncio.gather(spawn(manager, tmp_path), spawn(manager, tmp_path))
    assert {first.terminal_id, second.terminal_id} == {"term_000000000001", "term_000000000002"}
    assert manager.get_session(first.terminal_id, owner()) is first
    assert manager.get_session(second.terminal_id, owner()) is second


@pytest.mark.asyncio
async def test_kill_closes_reader_even_without_tree_eof(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, factory = terminal_manager
    monkeypatch.setattr(terminal_backend, "terminate_process_tree", lambda adapter, **_kwargs: None)
    # More kills than the reader pool has threads: a leaked blocked read would exhaust it.
    for _ in range(35):
        session = await manager.spawn(
            owner(), ["fake"], cwd=tmp_path, env=None, origin_run_id="run"
        )
        adapter = factory.adapters[-1]
        # The fake's read is still blocked, as when an escaped child holds
        # the PTY slave. Closing the transport must release it independently.
        await asyncio.wait_for(manager.kill_for_operator(session.terminal_id), 1)
        assert session.reader_task is not None and session.reader_task.done()
        adapter.alive = False
    assert (
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(manager._reader_executor, lambda: 42), 1
        )
        == 42
    )


class IdleTimeoutAdapter(FakeTerminalAdapter):
    """Reads like a real PTY adapter: an idle read times out instead of blocking."""

    def __init__(self) -> None:
        super().__init__()
        self.idle_reads = 0

    @override
    def read(self, _size: int) -> str:
        try:
            value = self._output.get(timeout=0.01)
        except queue.Empty:
            self.idle_reads += 1
            raise TimeoutError from None
        if value is None:
            raise EOFError
        return value


@pytest.mark.asyncio
async def test_reader_reads_on_after_idle_timeouts_and_stops_when_the_program_ends(
    tmp_path: Path,
) -> None:
    adapter = IdleTimeoutAdapter()
    manager = TerminalManager(adapter_factory=lambda *args: adapter, sweep_interval_seconds=3600)
    try:
        session = await spawn(manager, tmp_path)
        await eventually(lambda: adapter.idle_reads > 0)
        adapter.emit("after idle")
        await eventually(lambda: "after idle" in session.renderer.screen_text())

        # The program ends while the reader is idle, without closing the PTY.
        adapter.code = 3
        adapter.alive = False
        await eventually(lambda: session.state == "exited")
        assert session.reader_task.done()
        assert session.exit_code == 3
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_real_terminal_output_and_idle_reader_shutdown(tmp_path: Path) -> None:
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
        assert session.reader_task is not None and session.reader_task.done()
        assert not session.adapter.is_alive()
    finally:
        await asyncio.wait_for(manager.aclose(), 10)


@pytest.mark.asyncio
async def test_failed_tree_kill_retains_orphan_after_root_eof_for_retry(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    orphan = object()
    attempts: list[list[object]] = []

    def kill_tree(child, *, targets):  # type: ignore[no-untyped-def]
        assert child is adapter
        attempts.append(targets)
        if len(attempts) == 1:
            targets.append(orphan)
            adapter.finish(-1)
            raise PermissionError("descendant still running")
        assert targets == [orphan]
        assert not adapter.is_alive()

    monkeypatch.setattr(terminal_backend, "kill_process_tree", kill_tree)
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
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, factory = terminal_manager
    first = await spawn(manager, tmp_path)
    second = await spawn(manager, tmp_path)
    denied = True

    def kill_tree(child, *, targets):  # type: ignore[no-untyped-def]
        if child is factory.adapters[0] and denied:
            raise PermissionError("descendant still running")
        child.terminate()

    monkeypatch.setattr(terminal_backend, "kill_process_tree", kill_tree)
    sweeper = manager._sweeper_task
    assert sweeper is not None
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


@pytest.mark.asyncio
@pytest.mark.usefixtures("terminate_directly")
async def test_execution_group_stop_keeps_unrelated_terminal_after_attachment_transfer(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    execution = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    owned = await manager.spawn(
        owner(), ["fake"], cwd=tmp_path, env=None, origin_run_id="run", execution_owner=execution
    )
    unrelated = await manager.spawn(
        owner(), ["fake"], cwd=tmp_path, env=None, origin_run_id="other"
    )
    manager.detach(owned.terminal_id, owner())
    await manager.close_execution_group("fixture", "group", "epoch")
    assert not owned.adapter.is_alive()
    assert unrelated.adapter.is_alive()
    # The settled group's admission marker does not outlive its drain.
    assert manager._closed_execution_groups == set()


@pytest.mark.asyncio
@pytest.mark.usefixtures("terminate_directly")
async def test_execution_group_stop_drains_pending_terminal_launch(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    factory = AdapterFactory()

    def blocked_factory(*args):  # type: ignore[no-untyped-def]
        started.set()
        assert release.wait(5)
        return factory(*args)

    manager = TerminalManager(adapter_factory=blocked_factory)
    execution = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    launch = asyncio.create_task(
        manager.spawn(
            owner(),
            ["fake"],
            cwd=tmp_path,
            env=None,
            origin_run_id="run",
            execution_owner=execution,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        close = asyncio.create_task(manager.close_execution_group("fixture", "group", "epoch"))
        await asyncio.sleep(0)
        assert not close.done()
        # A launch racing the drain is rejected while admission is closed.
        with pytest.raises(TerminalClosedError):
            await manager.spawn(
                owner(),
                ["fake"],
                cwd=tmp_path,
                env=None,
                origin_run_id="late",
                execution_owner=execution,
            )
        release.set()
        session = await launch
        await close
        assert not session.adapter.is_alive()
        assert manager._closed_execution_groups == set()
    finally:
        release.set()
        await asyncio.gather(launch, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_terminal_completion_uses_activity_owner_without_transferring_process_lifetime(
    clocked_manager: tuple[TerminalManager, AdapterFactory, PendingTriggerService, FakeClock],
    tmp_path: Path,
) -> None:
    manager, factory, trigger, clock = clocked_manager
    execution = RunExecutionOwner("swarm", "group", "peer", "generation", "epoch")
    session = await establish_delivered_baseline(
        manager, factory, trigger, clock, tmp_path, quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS
    )
    manager.attach(
        session.terminal_id, owner(), origin_run_id="owned-run", execution_owner=execution
    )
    await manager.send_input(
        session.terminal_id,
        owner(),
        data="next\r",
        text=None,
        key=None,
        expected_screen_revision=None,
        origin_run_id="owned-run",
        execution_owner=execution,
    )
    generation = session.activity_generation
    factory.adapters[0].emit("new result")
    await settle_next_activity(
        clock, session, after_generation=generation, quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS
    )
    await eventually(lambda: len(trigger.submissions) == 2)
    assert trigger.submissions[-1][1]["execution_owner"] == execution
    assert session.execution_owner is None
    await manager.close_execution_group("swarm", "group", "epoch")
    assert session.adapter.is_alive()
