"""ProcessManager lifecycle: admission, scopes, shutdown, termination, and retention."""

from __future__ import annotations

import asyncio
import re
import sys
from contextlib import suppress
from datetime import timedelta

import psutil  # type: ignore[import-untyped]
import pytest

from core.runs import RunExecutionOwner
from core.tools import process_manager as process_manager_module
from core.tools.process_manager import (
    ProcessManager,
    ProcessManagerError,
    ProcessNotFoundError,
    ProcessTerminationError,
)
from tests.core.tools.process_manager_test_support import AGENT_A, SCOPE_A, finish, spawn
from tests.core.tools.process_manager_test_support import (
    manager as manager,
)


def delay_os_launch(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold every later OS process creation until the returned release event is set."""
    started = asyncio.Event()
    release = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    return started, release


def sweeper_running() -> bool:
    return any(
        task.get_name() == "process-manager-sweep" and not task.done()
        for task in asyncio.all_tasks()
    )


# --- admission, scopes, and shutdown -----------------------------------------


@pytest.mark.asyncio
async def test_execution_group_stop_keeps_unrelated_process_in_same_scope(manager):
    owner = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    owned = await spawn(manager, execution_owner=owner)
    unrelated = await spawn(manager)
    assert manager.has_execution_work(owner)
    await manager.close_execution_group("fixture", "group", "epoch")
    assert not manager.has_execution_work(owner)
    assert manager.get_process(owned, AGENT_A).status == "killed"
    assert manager.get_process(unrelated, AGENT_A).status == "running"
    # The group owner settles its Runs before closing resources, so a completed
    # drain retains no admission marker for the rest of the server lifetime.
    assert manager._closed_execution_groups == set()


@pytest.mark.asyncio
async def test_execution_group_stop_waits_for_pending_process_creation(manager, monkeypatch):
    started, release = delay_os_launch(monkeypatch)
    owner = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    launch = asyncio.create_task(spawn(manager, execution_owner=owner))
    await started.wait()
    close = asyncio.create_task(manager.close_execution_group("fixture", "group", "epoch"))
    await asyncio.sleep(0)
    assert not close.done()
    assert manager.has_execution_work(owner)
    # A launch racing the drain is rejected while the group closes.
    with pytest.raises(ProcessManagerError):
        await spawn(manager, execution_owner=owner)
    release.set()
    process_id = await launch
    await close
    assert manager.get_process(process_id, AGENT_A).status == "killed"
    assert not manager.has_execution_work(owner)
    assert manager._closed_execution_groups == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", ["run-scope", "shutdown"])
async def test_closing_kills_running_and_pending_launches_and_closes_admission(
    manager, monkeypatch, closing
):
    running_id = await spawn(manager)
    started, release = delay_os_launch(monkeypatch)
    launch = asyncio.create_task(spawn(manager))
    await started.wait()
    close = asyncio.create_task(
        manager.cancel_scope_async(SCOPE_A) if closing == "run-scope" else manager.aclose()
    )
    try:
        await asyncio.sleep(0)
        assert not close.done()
        release.set()
        launched_id = await launch
        await close
        for process_id in (running_id, launched_id):
            tracked = manager.get_process(process_id, AGENT_A)
            assert tracked.status == "killed"
            assert tracked.proc.returncode is not None
            assert tracked.wait_task is not None and tracked.wait_task.done()
        with pytest.raises(ProcessManagerError):
            await spawn(manager)
    finally:
        release.set()
        await asyncio.gather(launch, close, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_run_scope_cancellation_is_limited_to_its_scope_until_the_run_settles(manager):
    process_id = await spawn(manager)
    other_scope_id = await spawn(manager, scope_key="run-b")

    await manager.cancel_scope_async(SCOPE_A)

    assert manager.get_process(process_id, AGENT_A).status == "killed"
    assert manager.get_process(other_scope_id, AGENT_A).status == "running"
    # Until the Run settles, a launch racing its cancellation stays rejected.
    with pytest.raises(ProcessManagerError):
        await spawn(manager)

    manager.release_scope(SCOPE_A)

    assert manager._closed_scopes == set()
    # Releasing an unknown or already released scope is harmless.
    manager.release_scope(SCOPE_A)
    manager.release_scope("run-never-cancelled")
    assert manager._closed_scopes == set()


@pytest.mark.asyncio
async def test_synchronous_stop_kills_running_processes_and_retires_a_late_launch(
    manager, monkeypatch
):
    running_id = await spawn(manager)
    started, release = delay_os_launch(monkeypatch)
    launch = asyncio.create_task(spawn(manager))
    await started.wait()
    try:
        manager.stop()
        release.set()
        launched_id = await launch
        running = manager.get_process(running_id, AGENT_A)
        assert running.wait_task is not None
        await asyncio.wait_for(running.wait_task, timeout=5)
        for process_id in (running_id, launched_id):
            tracked = manager.get_process(process_id, AGENT_A)
            assert tracked.status == "killed"
            assert tracked.proc.returncode is not None
        with pytest.raises(ProcessManagerError):
            await spawn(manager)
    finally:
        release.set()
        await asyncio.gather(launch, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancel_during_os_launch_waits_and_kills_created_process(manager, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    original = asyncio.create_subprocess_exec
    processes = []

    async def delayed_return(*args, **kwargs):
        proc = await original(*args, **kwargs)
        processes.append(proc)
        started.set()
        await release.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_return)
    launch = asyncio.create_task(spawn(manager))
    await started.wait()
    try:
        launch.cancel()
        await asyncio.sleep(0)
        assert not launch.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await launch
        assert processes[0].returncode is not None
        assert manager.list_processes(AGENT_A)[0].status == "killed"
    finally:
        release.set()
        await asyncio.gather(launch, return_exceptions=True)
        for proc in processes:
            if proc.returncode is None:
                await process_manager_module.kill_process_tree_async(proc)
            await proc.wait()
        await manager.aclose()


@pytest.mark.asyncio
async def test_parallel_process_ids_skip_collisions(manager, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    first, second = await asyncio.gather(*(spawn(manager, "pass") for _ in range(2)))
    assert {first, second} == {"proc_000000000001", "proc_000000000002"}
    assert manager.get_process(first, AGENT_A).process_id == first
    assert manager.get_process(second, AGENT_A).process_id == second


@pytest.mark.asyncio
async def test_sweeper_removes_expired_processes_and_stops_with_the_manager() -> None:
    manager = ProcessManager(sweep_interval_seconds=3600, finished_process_ttl=timedelta(0))
    manager.start()
    try:
        finished_id = await spawn(manager, "print('done')")
        await finish(manager, finished_id)
        running_id = await spawn(manager)

        await manager.sweep_finished()

        with pytest.raises(ProcessNotFoundError):
            manager.get_process(finished_id, AGENT_A)
        assert manager.get_process(running_id, AGENT_A).status == "running"
    finally:
        await manager.aclose()
    # Closing awaits the cancelled periodic sweeper as well.
    assert not sweeper_running()


# --- termination -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("by_user", [False, True], ids=["kill", "cancel-for-user"])
async def test_kill_stops_the_process_and_records_whether_the_user_asked(manager, by_user):
    process_id = await spawn(manager)

    if by_user:
        await manager.cancel_for_user(process_id, AGENT_A)
    else:
        await manager.kill(process_id, AGENT_A)

    tracked = manager.get_process(process_id, AGENT_A)
    assert tracked.status == "killed"
    assert tracked.proc.returncode is not None
    assert tracked.finished_at is not None
    assert tracked.cancelled_by_user is by_user


@pytest.mark.asyncio
@pytest.mark.parametrize("kill_tree", [False, True])
async def test_kill_terminates_child_process_tree(
    manager: ProcessManager, tmp_path, kill_tree: bool
) -> None:
    child_release_path = tmp_path / "child-release.txt"
    child_survived_path = tmp_path / "child-survived.txt"
    child_script = (
        "import os, pathlib, time\n"
        f"release = pathlib.Path({str(child_release_path)!r})\n"
        "print(f'child-started:{os.getpid()}', flush=True)\n"
        "while not release.exists():\n"
        "    time.sleep(0.01)\n"
        f"pathlib.Path({str(child_survived_path)!r}).write_text('survived')\n"
        "print('child-released', flush=True)\n"
    )
    parent_script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        "time.sleep(30)"
    )
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", parent_script],
        env=None,
        cwd=tmp_path,
    )

    outcome, line = await manager.wait(
        process_id, AGENT_A, timeout_seconds=10, pattern=re.compile(r"^child-started:\d+\r?$")
    )
    assert outcome == "matched" and line is not None
    child = psutil.Process(int(line.split(":")[1]))
    try:
        assert child.is_running()
        if kill_tree:
            await manager.kill(process_id, AGENT_A)
        # A surviving child may write only after kill has returned. The
        # no-kill control proves that this signal really releases the child.
        child_release_path.write_text("release", encoding="utf-8")
        if kill_tree:
            with suppress(psutil.NoSuchProcess):
                assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
            assert not child_survived_path.exists()
        else:
            outcome, _line = await manager.wait(
                process_id, AGENT_A, timeout_seconds=10, pattern=re.compile(r"^child-released\r?$")
            )
            assert outcome == "matched"
            assert child_survived_path.read_text(encoding="utf-8") == "survived"
    finally:
        with suppress(psutil.NoSuchProcess):
            child.kill()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_kill_failure_keeps_process_retryable(manager, monkeypatch, shutdown):
    process_id = await spawn(manager)
    tracked = manager.get_process(process_id, AGENT_A)

    async def fail_async(proc, **kwargs):
        raise PermissionError("test-owned kill failure")

    def fail_sync(proc, **kwargs):
        raise PermissionError("test-owned kill failure")

    with monkeypatch.context() as patch:
        patch.setattr(process_manager_module, "kill_process_tree_async", fail_async)
        patch.setattr(manager, "_kill_process_tree", fail_sync)
        with pytest.raises(ProcessTerminationError):
            if shutdown:
                # Synchronous shutdown kill; aclose retries it after pending launches.
                manager.stop()
            else:
                await manager.cancel_scope_async(SCOPE_A)
        assert tracked.status == "running"
        assert tracked.proc.returncode is None
        assert tracked.finished_at is None
    if shutdown:
        await manager.aclose()
    else:
        await manager.cancel_scope_async(SCOPE_A)
    assert tracked.status == "killed"
    assert tracked.proc.returncode is not None


@pytest.mark.asyncio
async def test_failed_scope_kill_still_stops_other_processes(manager, monkeypatch):
    ids = [await spawn(manager) for _ in range(2)]
    first = manager.get_process(ids[0], AGENT_A)
    original = process_manager_module.kill_process_tree_async

    async def deny_first(proc, **kwargs):
        if proc is first.proc:
            raise PermissionError("test-owned failure")
        await original(proc, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(process_manager_module, "kill_process_tree_async", deny_first)
        with pytest.raises(ProcessTerminationError):
            await manager.cancel_scope_async(SCOPE_A)
    assert first.status == "running"
    assert manager.get_process(ids[1], AGENT_A).status == "killed"


@pytest.mark.asyncio
@pytest.mark.parametrize(("shutdown", "code"), [(False, 7), (True, 0)])
async def test_kill_during_reader_drain_preserves_real_exit(manager, monkeypatch, shutdown, code):
    draining = asyncio.Event()
    release = asyncio.Event()
    original = manager._await_reader_tasks

    async def delayed_readers(tracked):
        draining.set()
        await release.wait()
        await original(tracked)

    monkeypatch.setattr(manager, "_await_reader_tasks", delayed_readers)
    process_id = await spawn(manager, f"raise SystemExit({code})")
    tracked = manager.get_process(process_id, AGENT_A)
    await asyncio.wait_for(draining.wait(), 5)
    assert tracked.proc.returncode == code
    assert tracked.status == "running"
    kill = None
    try:
        if shutdown:
            manager.stop()
        else:
            kill = asyncio.create_task(manager.kill(process_id, AGENT_A))
            await asyncio.sleep(0)
        assert tracked.status == "running"
    finally:
        release.set()
        if kill is not None:
            await asyncio.wait_for(kill, 5)
        await asyncio.wait_for(tracked.wait_task, 5)
    assert tracked.status == ("completed" if code == 0 else "failed")
    assert tracked.exit_code == code
