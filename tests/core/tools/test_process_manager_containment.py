"""Process manager: containment behavior."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil  # type: ignore[import-untyped]
import pytest

from core.tools import process_manager as process_manager_module
from core.tools.process_manager import (
    ProcessManager,
)
from core.utils import processes as process_utils
from core.utils.processes import guarded_process_launch, subprocess_creation_flags
from tests.core.tools.process_manager_helpers import (
    AGENT_A,
    SCOPE_A,
)
from tests.core.tools.process_manager_helpers import (
    manager as manager,
)


def terminate_pid_forcibly(pid: int) -> None:
    """Really kill a test child so its wait task can settle after a fake kill."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(pid, signal.SIGKILL)


def test_windows_subprocess_creation_flags_hide_console_and_keep_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000, raising=False)

    assert subprocess_creation_flags(platform_name="nt") == 0x08000000
    assert (
        subprocess_creation_flags(
            new_process_group=True,
            breakaway=True,
            platform_name="nt",
        )
        == 0x09000200
    )
    assert (
        subprocess_creation_flags(
            new_process_group=True,
            platform_name="nt",
        )
        == 0x08000200
    )
    assert (
        subprocess_creation_flags(
            new_process_group=True,
            platform_name="posix",
        )
        == 0
    )


def test_guarded_posix_launch_wraps_exact_argv_and_lifetime_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_utils, "_POSIX_LIFETIME_READ_FD", 41)

    launch = guarded_process_launch(["bash", "-c", "echo exact"], platform_name="posix")

    assert launch.argv == (
        sys.executable,
        "-m",
        "core.utils.process_guardian",
        "--lifetime-fd",
        "41",
        "--",
        "bash",
        "-c",
        "echo exact",
    )
    assert launch.pass_fds == (41,)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_windows_job_kills_descendant_when_containment_owner_crashes(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    owner_code = (
        "import os,pathlib,subprocess,sys,time; "
        "from core.utils.processes import activate_process_containment; "
        "activate_process_containment(); "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(.25); os._exit(7)"
    )
    owner = subprocess.Popen([sys.executable, "-c", owner_code, str(pid_path)])

    assert owner.wait(timeout=10) == 7
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    for _ in range(50):
        if not psutil.pid_exists(child_pid):
            break
        time.sleep(0.1)

    assert not psutil.pid_exists(child_pid)


def test_unix_process_tree_kill_uses_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_signals: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 12345

        def kill(self) -> None:
            raise AssertionError("proc.kill should not be used when killpg succeeds")

    monkeypatch.setattr(
        process_utils,
        "os",
        SimpleNamespace(
            name="posix",
            killpg=lambda process_group_id, signal_number: sent_signals.append(
                (process_group_id, signal_number)
            ),
        ),
    )

    ProcessManager._kill_process_tree(FakeProcess())  # type: ignore[arg-type]

    assert sent_signals == [(12345, 9)]


@pytest.mark.parametrize("survives", [False, True])
def test_windows_tree_fallback_kills_and_verifies_captured_descendants(monkeypatch, survives):
    killed = []
    child = SimpleNamespace(kill=lambda: killed.append("child"))
    root = SimpleNamespace(
        is_running=lambda: True,
        children=lambda recursive: [child],
        kill=lambda: killed.append("root"),
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    monkeypatch.setattr(
        psutil, "wait_procs", lambda processes, timeout: ([], [child] if survives else [])
    )
    monkeypatch.setattr(process_utils, "os", SimpleNamespace(name="nt"))

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("taskkill", 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert process_utils.windows_taskkill_tree(12345) is (not survives)
    assert set(killed) == {"child", "root"}


def test_windows_process_tree_kill_runs_taskkill_windowless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = SimpleNamespace(is_running=lambda: True, children=lambda recursive: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: (processes, []))
    fallback_kills = 0
    run_kwargs: dict[str, Any] = {}

    class FakeProcess:
        pid = 12345

        def kill(self) -> None:
            nonlocal fallback_kills
            fallback_kills += 1

    def successful_taskkill(
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        run_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args, 0)

    expected_creation_flags = 0x08000000
    monkeypatch.setattr(process_utils, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        process_utils,
        "subprocess_creation_flags",
        lambda: expected_creation_flags,
    )
    monkeypatch.setattr("core.utils.processes.subprocess.run", successful_taskkill)

    ProcessManager._kill_process_tree(FakeProcess())  # type: ignore[arg-type]

    assert run_kwargs["creationflags"] == expected_creation_flags
    assert fallback_kills == 0


@pytest.mark.asyncio
async def test_kill_process_keeps_event_loop_responsive_during_windows_taskkill(
    manager: ProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows tree-kill must run off the loop, never freeze it.

    A stuck ``taskkill`` (up to its 5s timeout) previously blocked the whole
    event loop - every concurrent Run stalled behind one process kill.
    """
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    # Simulate the Windows branch on any host: spawn has already used the real
    # platform semantics, so only the kill path is faked.
    monkeypatch.setattr(os, "name", "nt")
    entered_taskkill = threading.Event()
    release_taskkill = threading.Event()

    def slow_taskkill(pid: int, **kwargs) -> bool:
        entered_taskkill.set()
        release_taskkill.wait(timeout=5)
        terminate_pid_forcibly(pid)
        return True

    monkeypatch.setattr(process_utils, "windows_taskkill_tree", slow_taskkill)

    heartbeat_ticks = 0
    heartbeat_done = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not heartbeat_done.is_set():
            await asyncio.sleep(0)
            heartbeat_ticks += 1

    heartbeat_task = asyncio.create_task(heartbeat())
    kill_task = asyncio.create_task(manager.kill(process_id, AGENT_A))
    try:
        # Wait until the kill primitive is actually blocked in its worker -
        # via a worker thread too, or this wait would freeze the loop itself.
        assert await asyncio.to_thread(entered_taskkill.wait, 5), "taskkill was never reached"

        ticks_while_blocked = heartbeat_ticks
        await asyncio.sleep(0.05)
        assert heartbeat_ticks > ticks_while_blocked, (
            "event loop froze while the process tree-kill was pending"
        )
    finally:
        release_taskkill.set()
        await kill_task
        heartbeat_done.set()
        await heartbeat_task

    assert manager.get_process(process_id, AGENT_A).status == "killed"


@pytest.mark.asyncio
async def test_cancel_scope_async_kills_only_its_own_scope(
    manager: ProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_a_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )
    scope_b_id = await manager.spawn(
        "run-b",
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    # Fake the Windows tree-kill so no real child is terminated mid-test;
    # teardown restores real platform semantics before the fixture cleanup.
    monkeypatch.setattr(os, "name", "nt")

    def fake_taskkill(pid: int, **kwargs) -> bool:
        # Terminate the child for real so its wait task settles, but report
        # through the faked primitive under test.
        terminate_pid_forcibly(pid)
        return True

    monkeypatch.setattr(process_utils, "windows_taskkill_tree", fake_taskkill)

    await manager.cancel_scope_async(SCOPE_A)

    assert manager.get_process(scope_a_id, AGENT_A).status == "killed"
    assert manager.get_process(scope_b_id, AGENT_A).status == "running"


@pytest.mark.asyncio
async def test_parallel_process_ids_skip_collisions(manager, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    first, second = await asyncio.gather(
        *(
            manager.spawn(SCOPE_A, AGENT_A, [sys.executable, "-c", "pass"], env=None, cwd=None)
            for _ in range(2)
        )
    )
    assert {first, second} == {"proc_000000000001", "proc_000000000002"}
    assert manager.get_process(first, AGENT_A).process_id == first
    assert manager.get_process(second, AGENT_A).process_id == second


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_kill_failure_keeps_process_retryable(manager, monkeypatch, synchronous):
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )
    tracked = manager.get_process(process_id, AGENT_A)

    async def fail_async(proc, **kwargs):
        raise PermissionError("test-owned kill failure")

    def fail_sync(proc, **kwargs):
        raise PermissionError("test-owned kill failure")

    with monkeypatch.context() as patch:
        patch.setattr(process_manager_module, "kill_process_tree_async", fail_async)
        patch.setattr(manager, "_kill_process_tree", fail_sync)
        with pytest.raises(process_manager_module.ProcessTerminationError):
            if synchronous:
                manager.cancel_scope(SCOPE_A)
            else:
                await manager.cancel_scope_async(SCOPE_A)
        assert tracked.status == "running"
        assert tracked.proc.returncode is None
        assert tracked.finished_at is None
    await manager.cancel_scope_async(SCOPE_A)
    assert tracked.status == "killed"
    assert tracked.proc.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("code", [0, 7])
async def test_kill_during_reader_drain_preserves_real_exit(
    manager, monkeypatch, synchronous, code
):
    draining = asyncio.Event()
    release = asyncio.Event()
    original = manager._await_reader_tasks

    async def delayed_readers(tracked):
        draining.set()
        await release.wait()
        await original(tracked)

    monkeypatch.setattr(manager, "_await_reader_tasks", delayed_readers)
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", f"raise SystemExit({code})"],
        env=None,
        cwd=None,
    )
    tracked = manager.get_process(process_id, AGENT_A)
    await asyncio.wait_for(draining.wait(), 5)
    assert tracked.proc.returncode == code
    assert tracked.status == "running"
    kill = None
    try:
        if synchronous:
            manager.cancel_scope(SCOPE_A)
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


def test_posix_tree_failure_does_not_hide_failure_by_killing_only_parent(monkeypatch):
    def fail(group, sig):
        raise PermissionError("test-owned denied process group")

    def forbidden():
        pytest.fail("direct-child fallback loses tree ownership")

    monkeypatch.setattr(process_utils, "os", SimpleNamespace(name="posix", killpg=fail))
    with pytest.raises(PermissionError):
        ProcessManager._kill_process_tree(SimpleNamespace(pid=123, kill=forbidden))


def test_windows_failed_tree_kill_retains_orphan_for_retry(monkeypatch):
    root_alive = True
    child_alive = True
    deny_child = True

    def kill_root():
        nonlocal root_alive
        root_alive = False

    def kill_child():
        nonlocal child_alive
        if deny_child:
            raise psutil.AccessDenied(2)
        child_alive = False

    child = SimpleNamespace(kill=kill_child)
    root = SimpleNamespace(
        is_running=lambda: root_alive, children=lambda recursive: [child], kill=kill_root
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    monkeypatch.setattr(
        psutil, "wait_procs", lambda processes, timeout: ([], [child] if child_alive else [])
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    targets = []
    assert not process_utils.windows_taskkill_tree(1, targets=targets)
    assert not root_alive and child_alive
    deny_child = False
    assert process_utils.windows_taskkill_tree(1, targets=targets)
    assert not child_alive


@pytest.mark.asyncio
async def test_failed_scope_kill_still_stops_other_processes(manager, monkeypatch):
    ids = [
        await manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=None,
            cwd=None,
        )
        for _ in range(2)
    ]
    first = manager.get_process(ids[0], AGENT_A)
    original = process_manager_module.kill_process_tree_async

    async def deny_first(proc, **kwargs):
        if proc is first.proc:
            raise PermissionError("test-owned failure")
        await original(proc, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(process_manager_module, "kill_process_tree_async", deny_first)
        with pytest.raises(process_manager_module.ProcessTerminationError):
            await manager.cancel_scope_async(SCOPE_A)
    assert first.status == "running"
    assert manager.get_process(ids[1], AGENT_A).status == "killed"
