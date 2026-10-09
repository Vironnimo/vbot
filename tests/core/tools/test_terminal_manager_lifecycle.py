"""Terminal manager: attachment scopes, capacity, launches in flight, kill and shutdown."""

from __future__ import annotations

import asyncio
import os
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, override

import psutil  # type: ignore[import-untyped]
import pytest

import core.tools.terminal_backend as terminal_backend
import core.tools.terminal_manager as terminal_module
from core.runs import RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.tools.terminal_manager import (
    TerminalAlreadyAttachedError,
    TerminalCapacityError,
    TerminalClosedError,
    TerminalManager,
    TerminalManagerError,
    TerminalNotOwnedError,
    TerminalOwner,
    TerminalRenderHost,
)
from tests.core.tools.terminal_manager_helpers import (
    TEST_ACTIVITY_QUIET_SECONDS,
    AdapterFactory,
    FakeClock,
    FakeTerminalAdapter,
    FakeTree,
    PendingTriggerService,
    establish_delivered_baseline,
    eventually,
    owner,
    screen_shows,
    settle_next_activity,
    spawn,
    terminal_info,
)
from tests.core.tools.terminal_manager_helpers import clocked_manager as clocked_manager
from tests.core.tools.terminal_manager_helpers import terminal_manager as terminal_manager


@pytest.fixture
def terminate_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a fake adapter directly instead of signalling a process tree."""
    monkeypatch.setattr(
        terminal_backend, "terminate_process_tree", lambda child, **_kwargs: child.terminate()
    )


def _manager(adapter_factory: Any) -> TerminalManager:
    return TerminalManager(
        adapter_factory=adapter_factory,
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
    )


@pytest.mark.asyncio
async def test_session_move_transfers_attachment_and_only_an_agent_terminals_lifecycle(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    agent_started = await spawn(manager, tmp_path)
    manual = await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
    manual_id = manual["terminal_id"]
    manager.attach(manual_id, owner(), origin_run_id="run-a")
    target = owner("session-b")
    with pytest.raises(TerminalNotOwnedError):
        manager.terminal(agent_started.terminal_id, target)

    assert manager.transfer_scope(owner(), target) == 2

    moved_agent = manager.terminal(agent_started.terminal_id, target)
    moved_manual = manager.terminal(manual_id, target)
    assert (moved_agent.owner, moved_agent.lifecycle_owner) == (owner(), target)
    assert (moved_manual.owner, moved_manual.lifecycle_owner) == (None, None)
    assert len(manager.list_terminals()) == 2

    await manager.close_scope(target)
    assert terminal_info(manager, manual_id).attachment is None
    assert factory.adapters[1].alive is True
    assert factory.adapters[0].alive is False


@pytest.mark.asyncio
async def test_identity_agent_rename_moves_only_that_agents_scopes_and_keeps_provenance(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    started_by = TerminalOwner(None, "coder", "session-a")
    agent_started = await manager.spawn(
        started_by, ["fake-tui"], cwd=tmp_path, env=None, origin_run_id="run-a"
    )
    manual = await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
    manager.attach(
        manual["terminal_id"], TerminalOwner(None, "coder", "session-b"), origin_run_id="run-b"
    )
    # A Project Team Agent of the same id and another Identity Agent keep theirs.
    unrelated = [
        await manager.spawn(scope, ["fake-tui"], cwd=tmp_path, env=None, origin_run_id="run-c")
        for scope in (TerminalOwner("project-a", "coder", "session-a"), owner())
    ]

    assert manager.transfer_agent_scope("coder", "researcher") == 2

    renamed = TerminalOwner(None, "researcher", "session-a")
    moved = terminal_info(manager, agent_started.terminal_id)
    assert (moved.owner, moved.lifecycle_owner, moved.attachment) == (started_by, renamed, renamed)
    moved_manual = terminal_info(manager, manual["terminal_id"])
    assert (moved_manual.lifecycle_owner, moved_manual.attachment) == (
        None,
        TerminalOwner(None, "researcher", "session-b"),
    )
    for info in unrelated:
        current = terminal_info(manager, info.terminal_id)
        assert (current.lifecycle_owner, current.attachment) == (info.owner, info.owner)
    # A reverted rename moves the same scopes back.
    assert manager.transfer_agent_scope("researcher", "coder") == 2
    assert terminal_info(manager, agent_started.terminal_id).attachment == started_by


@pytest.mark.asyncio
async def test_attach_rejects_another_session_and_detached_agent_origin_still_owns_lifecycle(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path)
    other = owner("session-b")

    with pytest.raises(TerminalAlreadyAttachedError):
        manager.attach(started.terminal_id, other, origin_run_id="run-b")

    manager.detach(started.terminal_id, owner())
    attached, changed = manager.attach(started.terminal_id, other, origin_run_id="run-b")
    assert changed is True
    assert (attached.owner, attached.lifecycle_owner, attached.attachment) == (
        owner(),
        owner(),
        other,
    )

    await manager.close_scope(other)
    assert factory.adapters[0].alive is True
    assert terminal_info(manager, started.terminal_id).attachment is None

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
        await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)


@pytest.mark.asyncio
@pytest.mark.usefixtures("terminate_directly")
async def test_pending_starts_reserve_owner_and_global_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    adapters: list[FakeTerminalAdapter] = []

    def factory(*_args: Any, **_kwargs: Any) -> FakeTerminalAdapter:
        adapter = FakeTerminalAdapter()
        adapters.append(adapter)
        assert release.wait(5)
        return adapter

    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_PER_SESSION", 2)
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_GLOBAL", 3)
    manager = _manager(factory)
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
        assert len(manager.list_terminals()) == 3

        # Completed starts hold no reservation: a stopped terminal frees its place.
        stopped = next(info for info in manager.list_terminals() if info.owner == owner())
        await manager.kill_for_operator(stopped.terminal_id)
        await spawn(manager, tmp_path)
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
    manager = _manager(lambda *_args, **_kwargs: adapter)
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        # A one-thread default executor: a blocked read there would stall every other call.
        monkeypatch.setattr(loop, "_default_executor", executor)
        try:
            started = await spawn(manager, tmp_path)
            await eventually(reading.is_set)
            await asyncio.wait_for(manager.send_operator_input(started.terminal_id, "hello"), 1)
            await asyncio.wait_for(
                manager.resize_for_operator(started.terminal_id, columns=90, rows=24), 1
            )
            await asyncio.wait_for(manager.kill_for_operator(started.terminal_id), 1)
            assert adapter.writes == ["hello"]
            assert adapter.resizes == [(24, 90)]
            assert not adapter.alive
        finally:
            adapter.finish()
            await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("terminate_directly")
@pytest.mark.parametrize("kind", ["terminal", "command"])
@pytest.mark.parametrize("paused_at", ["process", "log"])
async def test_cancelled_start_waits_for_child_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, paused_at: str
) -> None:
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    adapter = FakeTerminalAdapter()
    tree = FakeTree()
    tree.adapter = adapter
    log_paths: list[Path] = []

    def pause() -> None:
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        release.wait()

    class TemporaryFiles(TemporaryFileManager):
        @override
        def create(self, category: str, suffix: str) -> TemporaryFileLease:
            assert threading.get_ident() != loop_thread
            lease = super().create(category, suffix)
            log_paths.append(lease.path)
            if paused_at == "log":
                pause()
            return lease

    def factory(*_args: Any, **_kwargs: Any) -> FakeTerminalAdapter:
        if paused_at == "process":
            pause()
        return adapter

    temporary_files = TemporaryFiles(tmp_path)
    manager = TerminalManager(
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        temporary_files=temporary_files,
        process_tracker=lambda _pid: tree,
        sweep_interval_seconds=3600,
    )
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_PER_SESSION", 1)
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_COMMANDS", 1)

    async def start() -> Any:
        if kind == "terminal":
            return await spawn(manager, tmp_path)
        return await manager.spawn_command(
            owner(),
            ["fixture-shell"],
            command="build",
            description=None,
            cwd=tmp_path,
            env={},
            timeout_seconds=None,
            formatter=lambda _report: "finished",
            origin_run_id="run-a",
        )

    task = asyncio.create_task(start())
    waiting = asyncio.create_task(entered.wait())
    try:
        done, _pending = await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert waiting in done
        # The loop can serve another start while filesystem/process work is blocked;
        # the pending start still owns its capacity and its temporary file.
        with pytest.raises(TerminalCapacityError):
            await start()
        assert len(log_paths) == 1
        for _ in range(2):
            task.cancel()
            checkpoint = loop.create_future()
            loop.call_soon(checkpoint.set_result, None)
            await checkpoint
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not adapter.alive
        assert adapter.closed
        assert all(info.state == "exited" for info in manager.list_terminals())
        # Ending the lease releases the retained log for ordinary cleanup, with no
        # file handle left open (which would prevent deletion on Windows).
        for path in log_paths:
            os.utime(path, (0, 0))
        temporary_files.sweep()
        assert not any(path.exists() for path in log_paths)
    finally:
        release.set()
        waiting.cancel()
        await asyncio.gather(task, waiting, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("terminate_directly")
async def test_shutdown_waits_for_pending_start_and_closes_its_child(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    adapter = FakeTerminalAdapter()

    def factory(*_args: Any, **_kwargs: Any) -> FakeTerminalAdapter:
        entered.set()
        assert release.wait(5)
        return adapter

    manager = _manager(factory)
    task = asyncio.create_task(spawn(manager, tmp_path))
    try:
        await eventually(entered.is_set)
        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(closing, 1)
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], TerminalClosedError)
        assert not adapter.alive
        assert not manager.list_terminals()
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
    assert manager.terminal(first.terminal_id, owner()).terminal_id == first.terminal_id
    assert manager.terminal(second.terminal_id, owner()).terminal_id == second.terminal_id


@pytest.mark.asyncio
async def test_kill_closes_reader_even_without_tree_eof(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _factory = terminal_manager
    monkeypatch.setattr(terminal_backend, "terminate_process_tree", lambda adapter, **_kwargs: None)
    # More kills than the reader pool has threads: a leaked blocked read would
    # exhaust it, and the next kill would wait for its reader beyond the timeout.
    for _ in range(terminal_module.TERMINAL_MAX_LIVE_GLOBAL + 3):
        started = await manager.spawn(
            owner(), ["fake"], cwd=tmp_path, env=None, origin_run_id="run"
        )
        # The fake's read is still blocked, as when an escaped child holds
        # the PTY slave. Closing the transport must release it independently.
        killed = await asyncio.wait_for(manager.kill_for_operator(started.terminal_id), 1)
        assert killed["state"] == "exited"


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
    manager = _manager(lambda *_args, **_kwargs: adapter)
    try:
        started = await spawn(manager, tmp_path)
        await eventually(lambda: adapter.idle_reads > 0)
        adapter.emit("after idle")
        await eventually(lambda: screen_shows(manager, started.terminal_id, "after idle"))

        # The program ends while the reader is idle, without closing the PTY.
        adapter.code = 3
        adapter.alive = False
        await eventually(lambda: terminal_info(manager, started.terminal_id).state == "exited")
        assert terminal_info(manager, started.terminal_id).exit_code == 3
        # The finished Session releases its PTY (on Windows its console host and reader thread).
        assert adapter.closed
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_real_terminal_output_and_idle_reader_shutdown(tmp_path: Path) -> None:
    manager = TerminalManager(
        render_host=TerminalRenderHost.in_process(), activity_quiet_seconds=0.03
    )
    try:
        started = await manager.spawn(
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
            lambda: screen_shows(manager, started.terminal_id, "real-terminal-ready"),
            attempts=1000,
        )
        killed = await asyncio.wait_for(manager.kill_for_operator(started.terminal_id), 10)
        assert killed["state"] == "exited"
        assert not psutil.pid_exists(started.pid)
    finally:
        await asyncio.wait_for(manager.aclose(), 10)


@pytest.mark.asyncio
async def test_failed_tree_kill_retains_orphan_after_root_eof_for_retry(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    orphan = object()
    attempts: list[list[object]] = []

    def kill_tree(child: FakeTerminalAdapter, *, targets: list[object]) -> None:
        assert child is adapter
        attempts.append(targets)
        if len(attempts) == 1:
            targets.append(orphan)
            adapter.finish(-1)
            raise PermissionError("descendant still running")
        assert targets == [orphan]
        assert not adapter.is_alive()

    monkeypatch.setattr(terminal_backend, "kill_process_tree", kill_tree)
    with pytest.raises(TerminalManagerError, match="so they can still be running"):
        await manager.kill(started.terminal_id, owner())
    # The root's EOF reaches the reader; only its end shows it was handled.
    reader = manager._sessions[started.terminal_id]._reader_task
    assert reader is not None
    await asyncio.wait_for(asyncio.shield(reader), 2)
    retained = terminal_info(manager, started.terminal_id)
    assert retained.state not in {"exited", "error"}
    assert retained.finished_at is None

    await manager.kill(started.terminal_id, owner())
    assert attempts[0] is attempts[1]
    assert terminal_info(manager, started.terminal_id).state == "exited"


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

    def kill_tree(child: FakeTerminalAdapter, *, targets: list[object]) -> None:
        if child is factory.adapters[0] and denied:
            raise PermissionError("descendant still running")
        child.terminate()

    monkeypatch.setattr(terminal_backend, "kill_process_tree", kill_tree)
    with pytest.raises(TerminalManagerError, match=first.terminal_id):
        await manager.aclose()
    assert factory.adapters[0].alive
    assert not factory.adapters[1].alive
    assert terminal_info(manager, second.terminal_id).state == "exited"
    denied = False
    manager.stop()
    assert not factory.adapters[0].alive


@pytest.mark.asyncio
@pytest.mark.usefixtures("terminate_directly")
async def test_execution_group_stop_keeps_unrelated_terminal_after_attachment_transfer(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    execution = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    owned = await manager.spawn(
        owner(), ["fake"], cwd=tmp_path, env=None, origin_run_id="run", execution_owner=execution
    )
    await manager.spawn(owner(), ["fake"], cwd=tmp_path, env=None, origin_run_id="other")
    manager.detach(owned.terminal_id, owner())
    await manager.close_execution_group("fixture", "group", "epoch")
    assert not factory.adapters[0].alive
    assert factory.adapters[1].alive
    # The settled group's admission marker does not outlive its drain.
    await manager.spawn(
        owner(), ["fake"], cwd=tmp_path, env=None, origin_run_id="late", execution_owner=execution
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("terminate_directly")
async def test_execution_group_stop_drains_pending_terminal_launch(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    factory = AdapterFactory()

    def blocked_factory(*args: Any, **kwargs: Any) -> FakeTerminalAdapter:
        started.set()
        assert release.wait(5)
        return factory(*args, **kwargs)

    manager = _manager(blocked_factory)
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
        await launch
        await close
        assert not factory.adapters[0].alive
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
    delivered = await establish_delivered_baseline(
        manager, factory, trigger, clock, tmp_path, quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS
    )
    terminal_id = delivered.terminal_id
    manager.attach(terminal_id, owner(), origin_run_id="owned-run", execution_owner=execution)
    sent = await manager.send_input(
        terminal_id,
        owner(),
        data="next\r",
        text=None,
        key=None,
        expected_screen_revision=None,
        origin_run_id="owned-run",
        execution_owner=execution,
    )
    factory.adapters[0].emit("new result")
    await settle_next_activity(
        clock,
        manager,
        terminal_id,
        after_revision=sent["screen_revision"],
        quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
    )
    await eventually(lambda: len(trigger.submissions) == 2)
    assert trigger.submissions[-1][1]["execution_owner"] == execution
    # The activity owner never owns the process: its group neither waits for nor stops it.
    assert not manager.has_execution_work(execution)
    await manager.close_execution_group("swarm", "group", "epoch")
    assert factory.adapters[0].alive
