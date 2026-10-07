"""Native search children: worker ownership, cancellation, cleanup, and resource bounds."""

import asyncio
import contextlib
import io
import re
import subprocess
import sys
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import override

import psutil  # type: ignore[import-untyped]
import pytest

from core.tools._search_execution import (
    MAX_CHILD_MEMORY,
    NativeOutcome,
    SearchBoundError,
    native_lines,
)
from core.tools.search import SearchBudget
from core.tools.tools import run_tool_worker
from tests.core.tools.search_files_test_support import context, dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments, success, children",
    [
        # A content search counts in one pass and reads the page's lines in another.
        ({"pattern": "needle"}, True, 2),
        ({"pattern": "needle", "limit": 1}, True, 2),
        # A file type first reads the type list to match its globs in any case.
        ({"pattern": "needle", "args": ["-t", "py"]}, True, 3),
        ({"pattern": "needle", "output": "files"}, True, 1),
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
    result = await dispatch(tmp_path, arguments, cancel_registration_hook=hooks.append)

    assert result["ok"] is success, result
    assert len(created) == children
    assert len(hooks) == children
    assert finalized == created
    assert all(worker != threading.get_ident() for worker in finalized)
    # Callbacks can outlive a finished batch without keeping its native process
    # alive or accessing a handle that the worker has already released.
    for callback in hooks:
        callback()


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["monitor", "second_reader"])
async def test_native_setup_failure_terminates_and_releases_child(tmp_path, monkeypatch, failure):
    original = subprocess.Popen
    original_wait = original.wait
    start_thread = threading.Thread.start
    created = []
    finalized = []
    child_ids = []
    ended = []
    starts = 0

    def launch(_command, **kwargs):
        process = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        created.append(threading.get_ident())
        child_ids.append(process.pid)
        weakref.finalize(process, lambda: finalized.append(threading.get_ident()))
        return process

    def wait(process, *args, **kwargs):
        result = original_wait(process, *args, **kwargs)
        ended.append(process.pid)
        return result

    def failed_monitor(pid):
        raise RuntimeError("monitor setup failed")

    def start(reader):
        nonlocal starts
        if reader.daemon:
            starts += 1
            if starts == 2:
                raise RuntimeError("reader setup failed")
        start_thread(reader)

    monkeypatch.setattr(original, "wait", wait)
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
    # The worker waited until the killed child ended. Its PID is no evidence:
    # Windows lists an ended process while another program holds a handle to it.
    assert ended == child_ids


@pytest.mark.asyncio
async def test_stdout_eof_does_not_disable_search_timeout(tmp_path, monkeypatch):
    original = subprocess.Popen
    original_wait = original.wait
    finalized = []
    waiting = threading.Event()

    class TimeoutAfterOutput(SearchBudget):
        @override
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


def test_a_user_cancellation_kills_a_silent_child(tmp_path, monkeypatch):
    original = subprocess.Popen
    children = []
    hooks = []

    def launch(_command, **kwargs):
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("core.tools._search_execution.subprocess.Popen", launch)
    polls_while_running = []

    def cancelled_after_a_silent_wait():
        # The child never writes a line: the first poll after its launch waits
        # for output in vain, and the user cancels at the next poll.
        if children:
            polls_while_running.append(True)
        return len(polls_while_running) >= 2

    ctx = context(
        tmp_path,
        cancel_registration_hook=hooks.append,
        cancel_check_hook=cancelled_after_a_silent_wait,
    )
    lines = native_lines(Path(sys.executable), [], ctx, SearchBudget(ctx))
    with contextlib.closing(lines):
        assert list(lines) == []
    assert len(hooks) == 1
    assert children[0].poll() is not None


# A failed child reports its own diagnostics; without any, the Agent learns the exit code.
@pytest.mark.parametrize(
    ("exit_code", "diagnostics", "failure"),
    [
        (0, "", None),
        (2, "specific failure", "specific failure"),
        (2, "", "the search engine exited with code 2. Retry the call."),
    ],
)
def test_finished_child_is_drained_when_process_monitor_misses_it(
    tmp_path, monkeypatch, exit_code, diagnostics, failure
):
    child = SimpleNamespace(
        pid=1234,
        stdout=io.BytesIO(b"found\n"),
        stderr=io.BytesIO(diagnostics.encode()),
        returncode=exit_code,
        poll=lambda: exit_code,
        wait=lambda **_kwargs: exit_code,
    )

    def gone(pid):
        assert pid == child.pid
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(
        "core.tools._search_execution.subprocess.Popen", lambda *_args, **_kw: child
    )
    monkeypatch.setattr("core.tools._search_execution.psutil.Process", gone)
    ctx = context(tmp_path)
    lines = native_lines(Path(sys.executable), [], ctx, SearchBudget(ctx))
    assert next(lines).strip() == b"found"
    if failure:
        with pytest.raises(RuntimeError, match=re.escape(failure)):
            next(lines)
    else:
        assert list(lines) == []
    assert child.stdout.closed and child.stderr.closed


# Captured from ripgrep 14.1.0 on Linux, without its host name lines. A name with a
# line break splits its skip report across two lines.
_SKIP_REPORTS = (
    b"rg: DEBUG|rg::flags::parse|crates/core/flags/parse.rs:89: not reading config files "
    b"because --no-config is present\n"
    b"rg: DEBUG|rg::flags::hiargs|crates/core/flags/hiargs.rs:174: using 12 thread(s)\n"
    b"rg: DEBUG|ignore::walk|/usr/share/cargo/registry/ignore-0.4.22/src/walk.rs:1799: "
    b'ignoring ./build: Ignore(IgnoreMatch(Gitignore(Glob { from: Some("./.gitignore"), '
    b'original: "build/", actual: "**/build", is_whitelist: false, is_only_dir: true })))\n'
    b"rg: DEBUG|ignore::walk|/usr/share/cargo/registry/ignore-0.4.22/src/walk.rs:1799: "
    b"ignoring src/new\nline.txt: Ignore(IgnoreMatch(Gitignore(Glob { from: "
    b'Some("/tmp/tmp.MIMeBqH8Ym/.gitignore"), original: "new?line.txt", actual: '
    b'"**/new?line.txt", is_whitelist: false, is_only_dir: false })))\n'
)


def test_skip_reports_keep_a_name_with_a_line_break_whole(tmp_path, monkeypatch):
    child = SimpleNamespace(
        pid=1234,
        stdout=io.BytesIO(b""),
        stderr=io.BytesIO(_SKIP_REPORTS),
        returncode=0,
        poll=lambda: 0,
        wait=lambda **_kwargs: 0,
    )

    def gone(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(
        "core.tools._search_execution.subprocess.Popen", lambda *_args, **_kw: child
    )
    monkeypatch.setattr("core.tools._search_execution.psutil.Process", gone)
    outcome = NativeOutcome()
    lines = native_lines(
        Path(sys.executable), [], None, SearchBudget(None), cwd=tmp_path, outcome=outcome
    )

    assert list(lines) == []
    assert outcome.skipped == outcome.ignored == [b"./build", b"src/new\nline.txt"]
    assert outcome.diagnostics == ""


def test_child_memory_is_bounded_and_polled_at_an_interval(tmp_path, monkeypatch):
    original = subprocess.Popen
    polls: list[int] = []

    class Monitored:
        rss = 0

        def __init__(self, pid):
            pass

        def memory_info(self):
            polls.append(Monitored.rss)
            return SimpleNamespace(rss=Monitored.rss)

    def launch(_command, **kwargs):
        return original([sys.executable, "-c", "for i in range(2000): print(i)"], **kwargs)

    monkeypatch.setattr("core.tools._search_execution.subprocess.Popen", launch)
    monkeypatch.setattr("core.tools._search_execution.psutil.Process", Monitored)
    ctx = context(tmp_path)
    assert len(list(native_lines(Path(sys.executable), [], ctx, SearchBudget(ctx)))) == 2000
    # Polls follow elapsed time, not output volume.
    assert 1 <= len(polls) < 200
    Monitored.rss = MAX_CHILD_MEMORY + 1
    with pytest.raises(SearchBoundError) as stopped:
        list(native_lines(Path(sys.executable), [], ctx, SearchBudget(ctx)))
    assert stopped.value.bound == "memory"
