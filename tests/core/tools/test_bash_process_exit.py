"""Bash completion must not depend on EOF from long-lived descendants."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

import psutil  # type: ignore[import-untyped]
import pytest

import core.tools.bash as bash_module
from core.tools.bash import bash_handler
from tests.core.tools.bash_helpers import make_context, python_command
from tests.core.tools.bash_helpers import manager as manager
from tests.core.tools.bash_helpers import shell_env_cache as shell_env_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "background"])
@pytest.mark.parametrize("exit_code", [0, 7])
async def test_descendant_pipe_does_not_hide_exit_or_block_timeout(
    manager, tmp_path, monkeypatch, mode, exit_code, caplog
):
    caplog.set_level(logging.DEBUG, logger="vbot.tools.process_manager")
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    original_spawn = manager.spawn
    original_readers = manager._await_reader_tasks
    original_kill = manager.kill
    draining = asyncio.Event()
    deadline_entered = asyncio.Event()

    async def delayed_readers(tracked):
        draining.set()
        await deadline_entered.wait()
        await original_readers(tracked)

    async def spawn_exited(scope_key, agent_id, argv, **kwargs):
        process_id = await original_spawn(scope_key, agent_id, argv, **kwargs)
        # Observe real OS exit separately from pipe EOF before Bash starts its
        # deadline. Interpreter startup speed is not part of this regression.
        await draining.wait()
        return process_id

    async def observe_kill(process_id, agent_id, **kwargs):
        tracked = manager.get_process(process_id, agent_id, **kwargs)
        assert tracked.proc.returncode == exit_code
        assert tracked.status == "running"
        deadline_entered.set()
        await original_kill(process_id, agent_id, **kwargs)

    monkeypatch.setattr(manager, "spawn", spawn_exited)
    monkeypatch.setattr(manager, "_await_reader_tasks", delayed_readers)
    monkeypatch.setattr(manager, "kill", observe_kill)
    pid_path = tmp_path / "descendant.pid"
    command = (
        "import subprocess, sys; from pathlib import Path; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"Path({str(pid_path)!r}).write_text(str(child.pid)); "
        "print('parent-output', flush=True); "
        "print('parent-error', file=sys.stderr, flush=True); "
        f"raise SystemExit({exit_code})"
    )
    context = make_context(tmp_path)
    task = asyncio.create_task(
        bash_handler(
            context,
            {
                "command": command,
                "mode": mode,
                "timeout": 0.01,
            },
            manager,
        )
    )
    try:
        result = await asyncio.wait_for(asyncio.shield(task), 5)
        tracked = manager.list_processes(context.agent_id)[0]
        await asyncio.wait_for(asyncio.shield(tracked.wait_task), 5)
        assert deadline_entered.is_set()
        assert tracked.proc.returncode == exit_code
        assert tracked.exit_code == exit_code
        assert tracked.status == ("completed" if exit_code == 0 else "failed")
        assert not tracked.wait_task.cancelled()
        assert tracked.stdout_task.done() and tracked.stderr_task.done()
        assert not tracked.truncated
        diagnostics = [
            record
            for record in caplog.records
            if record.name == "vbot.tools.process_manager" and record.args == (tracked.process_id,)
        ]
        assert len(diagnostics) == 1
        assert diagnostics[0].levelno == logging.DEBUG
        assert result["ok"] is True
        if mode != "background":
            assert result["data"]["exit_code"] == exit_code
            assert "parent-output" in result["data"]["output"]
            assert "parent-error" in result["data"]["output"]
        # The CLI's own exit completes the command; no arbitrary descendant kill.
        assert psutil.Process(int(pid_path.read_text())).is_running()
    finally:
        deadline_entered.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pid_text = pid_path.read_text().strip() if pid_path.exists() else ""
        if pid_text.isdecimal():
            with contextlib.suppress(psutil.NoSuchProcess):
                child = psutil.Process(int(pid_text))
                child.kill()
                await asyncio.to_thread(child.wait, timeout=5)


@pytest.mark.asyncio
async def test_cancelling_kill_waiter_cannot_cancel_process_cleanup(manager, tmp_path, monkeypatch):
    context = make_context(tmp_path)
    draining = asyncio.Event()
    release = asyncio.Event()
    original = manager._await_reader_tasks

    async def delayed_readers(tracked):
        draining.set()
        await release.wait()
        await original(tracked)

    monkeypatch.setattr(manager, "_await_reader_tasks", delayed_readers)
    process_id = await manager.spawn(
        context.run_id,
        context.agent_id,
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=None,
        cwd=tmp_path,
    )
    tracked = manager.get_process(process_id, context.agent_id)
    kill = asyncio.create_task(manager.kill(process_id, context.agent_id))
    try:
        await asyncio.wait_for(draining.wait(), 5)
        # Bash cancels its timeout waiter only after the verified kill changes
        # status. OS exit can precede Windows tree verification completing.
        async with asyncio.timeout(5):
            while tracked.status == "running":
                await asyncio.sleep(0.01)
        kill.cancel()
        with pytest.raises(asyncio.CancelledError):
            await kill
        assert not tracked.wait_task.done()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(tracked.wait_task), 5)
    assert tracked.finished_at is not None
    assert tracked.status == "killed"
