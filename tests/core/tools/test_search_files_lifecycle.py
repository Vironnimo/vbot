"""Native search resources stay in their worker across completion and cancellation."""

import asyncio
import contextlib
import subprocess
import sys
import threading
import weakref
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest

from core.tools._search_execution import native_lines
from core.tools.search import SearchBudget
from core.tools.search_files import register_search_files_tool
from core.tools.tools import ToolRegistry, run_tool_worker
from tests.core.tools.test_search_files import context

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "arguments, success, children",
    [
        ({"pattern": "needle"}, True, 1),
        ({"pattern": "needle", "limit": 1}, True, 1),
        ({"pattern": "needle", "args": ["-t", "py"]}, True, 2),
        ({"pattern": "["}, False, 1),
    ],
)
async def test_native_process_finalizes_in_worker_with_cancel_callback_retained(
    tmp_path, monkeypatch, arguments, success, children
):
    (tmp_path / "sample.py").write_text("needle\n" * 200)
    original = subprocess.Popen
    created = []
    finalized = []
    hooks = []

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        created.append(threading.get_ident())
        weakref.finalize(process, lambda: finalized.append(threading.get_ident()))
        return process

    monkeypatch.setattr("core.tools._search_execution.subprocess.Popen", launch)
    registry = ToolRegistry()
    register_search_files_tool(registry)
    result = await registry.dispatch(
        context(tmp_path, cancel_registration_hook=hooks.append), arguments
    )

    assert result["ok"] is success, result
    assert len(created) == children
    assert len(hooks) == children
    assert finalized == created
    assert all(worker != threading.get_ident() for worker in finalized)
    # Callbacks can outlive a finished batch without keeping its native process
    # alive or accessing a handle that the worker has already released.
    for callback in hooks:
        callback()


async def test_cancellation_before_native_launch_starts_no_process(tmp_path, monkeypatch):
    def unexpected_launch(*args, **kwargs):
        pytest.fail("A cancelled native search must not launch a child")

    monkeypatch.setattr("core.tools._search_execution.subprocess.Popen", unexpected_launch)
    ctx = context(tmp_path, cancel_registration_hook=lambda callback: callback())
    assert (
        await run_tool_worker(
            lambda: list(native_lines(Path(sys.executable), [], ctx, SearchBudget(ctx)))
        )
        == []
    )


@pytest.mark.parametrize("finish", ["close", "error"])
async def test_retained_generator_or_error_does_not_retain_native_process(
    tmp_path, monkeypatch, finish
):
    original = subprocess.Popen
    created = []
    finalized = []
    retained = []

    def launch(_command, **kwargs):
        script = "import sys; print('line'); sys.exit(2)" if finish == "error" else "print('line')"
        process = original([sys.executable, "-c", script], **kwargs)
        created.append(threading.get_ident())
        weakref.finalize(process, lambda: finalized.append(threading.get_ident()))
        return process

    monkeypatch.setattr("core.tools._search_execution.subprocess.Popen", launch)
    ctx = context(tmp_path)

    def search():
        lines = native_lines(Path(sys.executable), [], ctx, SearchBudget(ctx))
        retained.append(lines)
        assert next(lines).strip() == b"line"
        if finish == "close":
            lines.close()
        else:
            try:
                list(lines)
            except RuntimeError as error:
                retained.append(error)
            else:
                pytest.fail("The native exit failure must be reported")

    await run_tool_worker(search)
    assert len(created) == 1
    assert finalized == created
    assert created[0] != threading.get_ident()


@pytest.mark.parametrize("when", ["launch", "silent", "stdout_closed"])
async def test_native_cancellation_kills_in_worker(tmp_path, monkeypatch, when):
    original = subprocess.Popen
    original_kill = original.kill
    original_wait = original.wait
    children = []
    created = []
    finalized = []
    hooks = []
    kills = []
    exits = []
    waiting = threading.Event()
    started = threading.Event()
    launch_cancelled = threading.Event()
    loop = asyncio.get_running_loop()
    script = "import time; time.sleep(30)"
    if when == "stdout_closed":
        script = "import os,time; os.close(1); time.sleep(30)"

    def kill(process):
        kills.append(threading.get_ident())
        original_kill(process)

    def wait(process, *args, **kwargs):
        if kwargs.get("timeout") == 0.05:
            # This short wait follows stdout EOF; cleanup waits use 2 seconds.
            assert process.poll() is None
            waiting.set()
        result = original_wait(process, *args, **kwargs)
        exits.append(result)
        return result

    def cancel_launch():
        hooks[-1]()
        launch_cancelled.set()

    def launch(_command, **kwargs):
        process = original([sys.executable, "-c", script], **kwargs)
        children.append(weakref.ref(process))
        created.append(threading.get_ident())
        weakref.finalize(process, lambda: finalized.append(threading.get_ident()))
        started.set()
        if when == "launch":
            # Cancellation reaches the already-registered signal before Popen
            # returns the process to the search worker.
            loop.call_soon_threadsafe(cancel_launch)
            assert launch_cancelled.wait(5)
        return process

    monkeypatch.setattr(original, "kill", kill)
    monkeypatch.setattr(original, "wait", wait)
    monkeypatch.setattr("core.tools._search_execution.subprocess.Popen", launch)
    ctx = context(tmp_path, cancel_registration_hook=hooks.append)

    def search():
        with contextlib.closing(
            native_lines(Path(sys.executable), [], ctx, SearchBudget(ctx))
        ) as lines:
            return list(lines)

    task = asyncio.create_task(run_tool_worker(search))
    try:
        if when != "launch":
            ready = waiting if when == "stdout_closed" else started
            assert await asyncio.to_thread(ready.wait, 5)
            hooks[-1]()
        assert await asyncio.wait_for(task, timeout=3) == []
        assert len(children) == 1
        assert exits
        assert kills and all(worker != threading.get_ident() for worker in kills)
        assert finalized == created
        assert children[0]() is None
        hooks[-1]()  # Late cancellation must also remain harmless.
    finally:
        for reference in children:
            child = reference()
            if child is not None and child.poll() is None:
                await asyncio.to_thread(child.kill)
                await asyncio.to_thread(child.wait)
        await task


@pytest.mark.parametrize("failure", ["monitor", "second_reader"])
async def test_native_setup_failure_terminates_and_releases_child(tmp_path, monkeypatch, failure):
    original = subprocess.Popen
    start_thread = threading.Thread.start
    created = []
    finalized = []
    child_ids = []
    starts = 0

    def launch(_command, **kwargs):
        process = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        created.append(threading.get_ident())
        child_ids.append(process.pid)
        weakref.finalize(process, lambda: finalized.append(threading.get_ident()))
        return process

    def failed_monitor(pid):
        raise RuntimeError("monitor setup failed")

    def start(reader):
        nonlocal starts
        if reader.daemon:
            starts += 1
            if starts == 2:
                raise RuntimeError("reader setup failed")
        start_thread(reader)

    monkeypatch.setattr("core.tools._search_execution.subprocess.Popen", launch)
    if failure == "monitor":
        monkeypatch.setattr("core.tools._search_execution.psutil.Process", failed_monitor)
    else:
        monkeypatch.setattr(threading.Thread, "start", start)
    ctx = context(tmp_path)
    # Keep the exception (and its worker traceback) alive on the Event Loop.
    with pytest.raises(RuntimeError, match="setup failed") as error:
        await run_tool_worker(
            lambda: list(native_lines(Path(sys.executable), [], ctx, SearchBudget(ctx)))
        )
    assert error.value is not None
    assert len(created) == 1
    assert finalized == created
    assert created[0] != threading.get_ident()
    assert not psutil.pid_exists(child_ids[0])


async def test_stdout_eof_does_not_disable_search_timeout(tmp_path, monkeypatch):
    original = subprocess.Popen
    original_wait = original.wait
    finalized = []
    waiting = threading.Event()

    class TimeoutAfterOutput(SearchBudget):
        def keep_going(self):
            # Native execution enters its wait phase only after stdout EOF.
            # Expire there, independently of child startup or machine load.
            self.timed_out = waiting.is_set()
            return not self.timed_out

    def wait(process, *args, **kwargs):
        if kwargs.get("timeout") == 0.05:
            assert process.poll() is None
            waiting.set()
        return original_wait(process, *args, **kwargs)

    def launch(_command, **kwargs):
        process = original(
            [sys.executable, "-c", "import os,time; os.close(1); time.sleep(30)"], **kwargs
        )
        weakref.finalize(process, lambda: finalized.append(threading.get_ident()))
        return process

    monkeypatch.setattr(original, "wait", wait)
    monkeypatch.setattr("core.tools._search_execution.subprocess.Popen", launch)
    ctx = context(tmp_path)
    with pytest.raises(RuntimeError, match="Search timed out"):
        await run_tool_worker(
            lambda: list(native_lines(Path(sys.executable), [], ctx, TimeoutAfterOutput(ctx)))
        )
    assert waiting.is_set()
    assert len(finalized) == 1
    assert finalized[0] != threading.get_ident()
