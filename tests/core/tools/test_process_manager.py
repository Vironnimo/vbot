"""Process manager: lifecycle behavior."""

from __future__ import annotations

import asyncio
import re
import sys
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import psutil  # type: ignore[import-untyped]
import pytest

from core.runs import RunExecutionOwner
from core.tools import process_manager as process_manager_module
from core.tools.process_manager import (
    ProcessManager,
    ProcessNotFoundError,
)
from tests.core.tools.process_manager_helpers import (
    AGENT_A,
    SCOPE_A,
    as_text,
    poll_until_terminal,
)
from tests.core.tools.process_manager_helpers import (
    manager as manager,
)


@pytest.mark.asyncio
async def test_execution_group_stop_keeps_unrelated_process_in_same_scope(manager):
    owner = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    owned = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        argv,
        env=None,
        cwd=None,
        execution_owner=owner,
    )
    unrelated = await manager.spawn(SCOPE_A, AGENT_A, argv, env=None, cwd=None)
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
    started = asyncio.Event()
    release = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def blocked_spawn(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked_spawn)
    owner = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    launch = asyncio.create_task(
        manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=None,
            cwd=None,
            execution_owner=owner,
        )
    )
    await started.wait()
    close = asyncio.create_task(manager.close_execution_group("fixture", "group", "epoch"))
    await asyncio.sleep(0)
    assert not close.done()
    assert manager.has_execution_work(owner)
    # A launch racing the drain is rejected while the group closes.
    with pytest.raises(process_manager_module.ProcessManagerError):
        await manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=None,
            cwd=None,
            execution_owner=owner,
        )
    release.set()
    process_id = await launch
    await close
    assert manager.get_process(process_id, AGENT_A).status == "killed"
    assert not manager.has_execution_work(owner)
    assert manager._closed_execution_groups == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("scope_only", [False, True])
async def test_shutdown_waits_for_pending_launch_and_closes_admission(
    manager, monkeypatch, owned, scope_only
):
    started = asyncio.Event()
    release = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    owner = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch") if owned else None
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    launch = asyncio.create_task(
        manager.spawn(SCOPE_A, AGENT_A, argv, env=None, cwd=None, execution_owner=owner)
    )
    await started.wait()
    close = asyncio.create_task(
        manager.cancel_scope_async(SCOPE_A) if scope_only else manager.aclose()
    )
    try:
        await asyncio.sleep(0)
        assert not close.done()
        release.set()
        process_id = await launch
        await close
        tracked = manager.get_process(process_id, AGENT_A)
        assert tracked.proc.returncode is not None
        assert tracked.status == "killed"
        with pytest.raises(process_manager_module.ProcessManagerError):
            await manager.spawn(SCOPE_A, AGENT_A, argv, env=None, cwd=None)
    finally:
        release.set()
        await asyncio.gather(launch, close, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_settled_run_scope_releases_its_closed_marker(manager):
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    process_id = await manager.spawn(SCOPE_A, AGENT_A, argv, env=None, cwd=None)

    await manager.cancel_scope_async(SCOPE_A)

    assert manager.get_process(process_id, AGENT_A).status == "killed"
    # Until the Run settles, a launch racing its cancellation stays rejected.
    with pytest.raises(process_manager_module.ProcessManagerError):
        await manager.spawn(SCOPE_A, AGENT_A, argv, env=None, cwd=None)

    manager.release_scope(SCOPE_A)

    assert manager._closed_scopes == set()
    # Releasing an unknown or already released scope is harmless.
    manager.release_scope(SCOPE_A)
    manager.release_scope("run-never-cancelled")
    assert manager._closed_scopes == set()


@pytest.mark.asyncio
async def test_synchronous_stop_retires_a_late_launch(manager, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    launch = asyncio.create_task(manager.spawn(SCOPE_A, AGENT_A, argv, env=None, cwd=None))
    await started.wait()
    try:
        manager.stop()
        release.set()
        process_id = await launch
        tracked = manager.get_process(process_id, AGENT_A)
        assert tracked.status == "killed"
        assert tracked.proc.returncode is not None
        with pytest.raises(process_manager_module.ProcessManagerError):
            await manager.spawn(SCOPE_A, AGENT_A, argv, env=None, cwd=None)
    finally:
        release.set()
        await asyncio.gather(launch, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [False, True])
async def test_cancel_during_os_launch_waits_and_kills_created_process(manager, monkeypatch, owned):
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
    owner = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch") if owned else None
    launch = asyncio.create_task(
        manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=None,
            cwd=None,
            execution_owner=owner,
        )
    )
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
async def test_spawn_captures_stdout_and_stderr(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [
            sys.executable,
            "-c",
            "import sys; print('hello'); print('problem', file=sys.stderr)",
        ],
        env=None,
        cwd=None,
    )

    result = await poll_until_terminal(manager, process_id)

    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "hello" in as_text(result["stdout"])
    assert "problem" in as_text(result["stderr"])
    assert result["output"]


@pytest.mark.asyncio
async def test_process_access_requires_matching_project_scope(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        project_id="project-a",
        env=None,
        cwd=None,
    )

    with pytest.raises(ProcessNotFoundError):
        manager.get_process(process_id, AGENT_A)
    with pytest.raises(ProcessNotFoundError):
        manager.get_process(process_id, AGENT_A, project_id="project-b")

    assert manager.get_process(process_id, AGENT_A, project_id="project-a").process_id == process_id
    await manager.kill(process_id, AGENT_A, project_id="project-a")


@pytest.mark.asyncio
async def test_output_is_stripped_of_ansi_escape_sequences(manager: ProcessManager) -> None:
    # A model must never see raw escape codes — it copies them into file writes.
    # The colored/title markers are removed while the visible text survives, in
    # both the streamed poll output and the full log buffer.
    script = (
        "import sys; esc = chr(27); bel = chr(7); "
        "sys.stdout.write(f'{esc}[31mred{esc}[0m and {esc}]0;title{bel}done\\n')"
    )
    process_id = await manager.spawn(
        SCOPE_A, AGENT_A, [sys.executable, "-c", script], env=None, cwd=None
    )

    result = await poll_until_terminal(manager, process_id)
    log_result = await manager.log(process_id, AGENT_A)

    streamed = as_text(result["output"])
    logged = as_text(log_result["output"])
    assert "red and done" in streamed
    assert "red and done" in logged
    for surfaced in (streamed, logged):
        assert "\x1b" not in surfaced
        assert "[31m" not in surfaced
        assert "title" not in surfaced  # the OSC title payload is stripped too


@pytest.mark.asyncio
async def test_buffer_cap_drops_oldest_bytes_and_marks_truncated(tmp_path) -> None:
    manager = ProcessManager(buffer_cap_bytes=32, sweep_interval_seconds=3600)
    try:
        script = "import sys; sys.stdout.write('a' * 64); sys.stdout.flush()"
        process_id = await manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", script],
            env=None,
            cwd=tmp_path,
        )

        result = await poll_until_terminal(manager, process_id)
        log_result = await manager.log(process_id, AGENT_A)

        assert result["status"] == "completed"
        assert log_result["truncated"] is True
        assert log_result["output"] == "a" * 32
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_sweep_finished_removes_expired_processes(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('done')"],
        env=None,
        cwd=None,
    )
    await poll_until_terminal(manager, process_id)
    tracked = manager.get_process(process_id, AGENT_A)
    tracked.finished_at = datetime.now(UTC) - timedelta(minutes=31)

    await manager.sweep_finished()

    with pytest.raises(ProcessNotFoundError):
        manager.get_process(process_id, AGENT_A)


@pytest.mark.asyncio
async def test_cancel_scope_kills_active_processes(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    await manager.cancel_scope_async(SCOPE_A)
    result = await manager.poll(process_id, AGENT_A, timeout_ms=5000)

    assert result["status"] == "killed"


@pytest.mark.asyncio
async def test_stop_kills_active_processes(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )
    tracked = manager.get_process(process_id, AGENT_A)

    manager.stop()
    assert tracked.wait_task is not None
    await asyncio.wait_for(tracked.wait_task, timeout=5)
    result = await manager.poll(process_id, AGENT_A, timeout_ms=5000)

    assert result["status"] == "killed"
    assert tracked.proc.returncode is not None


@pytest.mark.asyncio
async def test_aclose_awaits_process_cleanup(manager: ProcessManager) -> None:
    manager.start()
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )
    tracked = manager.get_process(process_id, AGENT_A)

    await manager.aclose()

    assert manager._sweeper_task is None
    assert tracked.status == "killed"
    assert tracked.proc.returncode is not None
    assert tracked.wait_task is not None and tracked.wait_task.done()


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
