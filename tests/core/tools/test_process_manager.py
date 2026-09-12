"""Process manager: lifecycle behavior."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

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
    with pytest.raises(process_manager_module.ProcessManagerError):
        await manager.spawn(
            SCOPE_A,
            AGENT_A,
            argv,
            env=None,
            cwd=None,
            execution_owner=owner,
        )


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
    release.set()
    process_id = await launch
    await close
    assert manager.get_process(process_id, AGENT_A).status == "killed"
    assert not manager.has_execution_work(owner)


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

    manager.cancel_scope(SCOPE_A)
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
async def test_kill_terminates_child_process_tree(manager: ProcessManager, tmp_path) -> None:
    child_started_path = tmp_path / "child-started.txt"
    child_survived_path = tmp_path / "child-survived.txt"
    child_script = (
        "import pathlib, time; "
        f"pathlib.Path({str(child_started_path)!r}).write_text('started'); "
        "time.sleep(1); "
        f"pathlib.Path({str(child_survived_path)!r}).write_text('survived')"
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

    for _ in range(20):
        if child_started_path.exists():
            break
        await asyncio.sleep(0.05)
    assert child_started_path.exists()

    await manager.kill(process_id, AGENT_A)
    await asyncio.sleep(1.2)

    assert not child_survived_path.exists()
