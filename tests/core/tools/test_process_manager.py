"""Tests for async background-process tracking."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from core.storage import TemporaryFileManager
from core.tools import process_manager as process_manager_module
from core.tools.process_manager import (
    PROCESS_BUFFER_CAP_BYTES,
    PROCESS_TERMINAL_OUTPUT_CAP_CHARS,
    ProcessInputClosedError,
    ProcessManager,
    ProcessNotFoundError,
    guarded_process_launch,
    subprocess_creation_flags,
)

PollResult = dict[str, object]

AGENT_A = "agent-a"
AGENT_B = "agent-b"
SCOPE_A = "run-a"


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


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[ProcessManager]:
    manager = ProcessManager(sweep_interval_seconds=3600)
    try:
        yield manager
    finally:
        await manager.aclose()


async def poll_until_terminal(
    manager: ProcessManager,
    process_id: str,
    *,
    agent_id: str = AGENT_A,
) -> PollResult:
    combined_result: PollResult = {}
    stdout = ""
    stderr = ""
    output = ""
    for _ in range(20):
        result = await manager.poll(process_id, agent_id, timeout_ms=500)
        stdout += as_text(result["stdout"])
        stderr += as_text(result["stderr"])
        output += as_text(result["output"])
        combined_result = dict(result)
        combined_result["stdout"] = stdout
        combined_result["stderr"] = stderr
        combined_result["output"] = output
        if result["status"] != "running":
            return combined_result

    return combined_result


def as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


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


@pytest.mark.asyncio
async def test_poll_timeout_waits_for_new_output(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [
            sys.executable,
            "-c",
            "import sys, time; time.sleep(0.1); print('later'); sys.stdout.flush()",
        ],
        env=None,
        cwd=None,
    )

    result = await manager.poll(process_id, AGENT_A, timeout_ms=2000)

    assert "later" in as_text(result["stdout"])


@pytest.mark.asyncio
async def test_poll_does_not_lose_output_that_arrives_before_event_clear(
    manager: ProcessManager,
) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )
    tracked = manager.get_process(process_id, AGENT_A)
    original_event = tracked.output_event

    class RaceEvent:
        def __init__(self) -> None:
            self.injected = False

        def clear(self) -> None:
            if not self.injected:
                self.injected = True
                manager._append_output(tracked, "stdout", b"raced")
            original_event.clear()

        async def wait(self) -> bool:
            return await original_event.wait()

        def set(self) -> None:
            original_event.set()

    tracked.output_event = RaceEvent()  # type: ignore[assignment]

    started_at = time.monotonic()
    result = await manager.poll(process_id, AGENT_A, timeout_ms=5000)
    elapsed = time.monotonic() - started_at

    await manager.kill(process_id, AGENT_A)

    assert result["stdout"] == "raced"
    assert elapsed < 1


@pytest.mark.asyncio
async def test_poll_timeout_returns_empty_when_no_output_arrives(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(1)"],
        env=None,
        cwd=None,
    )

    result = await manager.poll(process_id, AGENT_A, timeout_ms=20)
    await manager.kill(process_id, AGENT_A)

    assert result["status"] == "running"
    assert result["output"] == ""


@pytest.mark.asyncio
async def test_send_input_kill_and_list_processes(manager: ProcessManager) -> None:
    script = "import sys; line = sys.stdin.readline(); print('got:' + line.strip())"
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", script],
        env=None,
        cwd=None,
    )

    await manager.send_input(
        process_id,
        AGENT_A,
        "value",
        newline=True,
        eof=False,
    )
    result = await poll_until_terminal(manager, process_id)

    assert result["status"] == "completed"
    assert "got:value" in as_text(result["stdout"])
    assert [tracked.process_id for tracked in manager.list_processes(AGENT_A)] == [process_id]

    await manager.kill(process_id, AGENT_A)

    assert [tracked.process_id for tracked in manager.list_processes(AGENT_A)] == [process_id]


@pytest.mark.asyncio
async def test_send_input_with_eof_closes_stdin(manager: ProcessManager) -> None:
    script = "import sys; data = sys.stdin.read(); print('read:' + data)"
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", script],
        env=None,
        cwd=None,
    )

    await manager.send_input(
        process_id,
        AGENT_A,
        "payload",
        newline=False,
        eof=True,
    )
    result = await poll_until_terminal(manager, process_id)

    assert result["status"] == "completed"
    assert "read:payload" in as_text(result["stdout"])


@pytest.mark.asyncio
async def test_send_input_translates_raced_stdin_close_to_input_closed_error(
    manager: ProcessManager,
) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        env=None,
        cwd=None,
    )
    tracked = manager.get_process(process_id, AGENT_A)
    assert tracked.proc.stdin is not None

    async def raise_connection_reset() -> None:
        raise ConnectionResetError("peer closed")

    # Simulate a kill / process exit closing stdin while drain() awaits.
    tracked.proc.stdin.drain = raise_connection_reset  # type: ignore[method-assign]

    with pytest.raises(ProcessInputClosedError, match=process_id):
        await manager.send_input(
            process_id,
            AGENT_A,
            "value",
            newline=False,
            eof=False,
        )
    assert tracked.stdin_open is False

    await manager.kill(process_id, AGENT_A)


@pytest.mark.asyncio
async def test_completed_process_closes_stdin_writer(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('done')"],
        env=None,
        cwd=None,
    )

    result = await poll_until_terminal(manager, process_id)
    tracked = manager.get_process(process_id, AGENT_A)

    assert result["status"] == "completed"
    assert tracked.proc.stdin is not None
    assert tracked.proc.stdin.is_closing() is True
    transport = getattr(tracked.proc, "_transport", None)
    pipes = getattr(transport, "_pipes", None)
    if isinstance(pipes, dict):
        assert pipes == {}


@pytest.mark.asyncio
async def test_reader_task_failure_is_logged(
    manager: ProcessManager, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(tracked: Any, stream_name: str) -> None:
        raise RuntimeError("reader exploded")

    manager._read_stream = boom  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="vbot.tools.process_manager"):
        process_id = await manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "print('done')"],
            env=None,
            cwd=None,
        )
        tracked = manager.get_process(process_id, AGENT_A)
        assert tracked.stdout_task is not None
        assert tracked.stderr_task is not None
        await asyncio.gather(
            tracked.stdout_task,
            tracked.stderr_task,
            return_exceptions=True,
        )
        await asyncio.sleep(0)

    reader_errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and "reader failed" in record.getMessage()
    ]
    assert reader_errors, "expected an error log for the failing stream reader task"
    assert reader_errors[0].exc_info is not None


@pytest.mark.asyncio
async def test_watcher_task_failure_is_logged(
    manager: ProcessManager, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(tracked: Any) -> None:
        raise RuntimeError("watcher exploded")

    manager._watch_process = boom  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="vbot.tools.process_manager"):
        process_id = await manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=None,
            cwd=None,
        )
        tracked = manager.get_process(process_id, AGENT_A)
        assert tracked.wait_task is not None
        await asyncio.gather(tracked.wait_task, return_exceptions=True)
        await asyncio.sleep(0)
        await manager.kill(process_id, AGENT_A)

    watcher_errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and "completion watcher failed" in record.getMessage()
    ]
    assert watcher_errors, "expected an error log for the failing completion watcher task"
    assert watcher_errors[0].exc_info is not None


@pytest.mark.asyncio
async def test_kill_logs_warning_when_taskkill_fails(
    manager: ProcessManager, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":
        pytest.skip("taskkill fallback path is Windows-only")

    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    def failing_taskkill(*args: Any, **kwargs: Any) -> Any:
        raise OSError("taskkill missing")

    monkeypatch.setattr(subprocess, "run", failing_taskkill)

    with caplog.at_level(logging.WARNING, logger="vbot.tools.process_manager"):
        await manager.kill(process_id, AGENT_A)

    assert any("taskkill failed" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_kill_stops_process(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    await manager.kill(process_id, AGENT_A)
    result = await manager.poll(process_id, AGENT_A, timeout_ms=5000)

    assert result["status"] == "killed"
    tracked = manager.get_process(process_id, AGENT_A)
    assert tracked.status == "killed"
    assert tracked.cancelled_by_user is False


@pytest.mark.asyncio
async def test_cancel_for_user_retains_explicit_user_origin(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    tracked = await manager.cancel_for_user(process_id, AGENT_A)

    assert tracked.status == "killed"
    assert tracked.cancelled_by_user is True


@pytest.mark.asyncio
async def test_agent_isolation_for_access_methods(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    with pytest.raises(ProcessNotFoundError):
        await manager.poll(process_id, AGENT_B, timeout_ms=0)
    with pytest.raises(ProcessNotFoundError):
        await manager.log(process_id, AGENT_B)
    with pytest.raises(ProcessNotFoundError):
        await manager.snapshot(process_id, AGENT_B)
    with pytest.raises(ProcessNotFoundError):
        await manager.send_input(
            process_id,
            AGENT_B,
            "data",
            newline=True,
            eof=False,
        )
    with pytest.raises(ProcessNotFoundError):
        await manager.kill(process_id, AGENT_B)

    assert manager.list_processes(AGENT_B) == []
    assert [tracked.process_id for tracked in manager.list_processes(AGENT_A)] == [process_id]

    await manager.kill(process_id, AGENT_A)


@pytest.mark.asyncio
async def test_log_returns_windowed_combined_output(manager: ProcessManager) -> None:
    script = "import sys; sys.stdout.write('one\\ntwo\\nthree\\n'); sys.stdout.flush()"
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", script],
        env=None,
        cwd=None,
    )
    await poll_until_terminal(manager, process_id)

    result = await manager.log(process_id, AGENT_A, offset=1, limit=1)

    assert as_text(result["output"]).replace("\r\n", "\n") == "two\n"
    assert result["total_lines"] == 3


@pytest.mark.asyncio
async def test_foreground_capture_can_be_stopped(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import sys; print('foreground'); sys.stdout.flush()"],
        env=None,
        cwd=None,
    )
    await poll_until_terminal(manager, process_id)

    manager.mark_backgrounded(process_id, AGENT_A)
    tracked = manager.get_process(process_id, AGENT_A)

    assert b"foreground" in b"".join(tracked.stdout_lines)
    assert tracked.foreground_capture_open is False


@pytest.mark.asyncio
async def test_foreground_capture_is_bounded_by_buffer_cap(tmp_path) -> None:
    manager = ProcessManager(buffer_cap_bytes=32, sweep_interval_seconds=3600)
    try:
        process_id = await manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "import sys; sys.stdout.write('a' * 64); sys.stdout.flush()"],
            env=None,
            cwd=tmp_path,
        )
        await poll_until_terminal(manager, process_id)
        tracked = manager.get_process(process_id, AGENT_A)

        assert len(b"".join(tracked.stdout_lines)) == 32
        assert b"".join(tracked.stderr_lines) == b""
        assert tracked.truncated is True
    finally:
        await manager.aclose()


def test_buffer_cap_default_is_500_kb() -> None:
    assert PROCESS_BUFFER_CAP_BYTES == 500 * 1024


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
    monkeypatch.setattr(process_manager_module, "_POSIX_LIFETIME_READ_FD", 41)

    launch = guarded_process_launch(["bash", "-c", "echo exact"], platform_name="posix")

    assert launch.argv == (
        sys.executable,
        "-m",
        "core.tools.process_guardian",
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
        "from core.tools.process_manager import activate_process_containment; "
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
        process_manager_module,
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


def test_windows_process_tree_kill_falls_back_when_taskkill_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_kills = 0

    class FakeProcess:
        pid = 12345

        def kill(self) -> None:
            nonlocal fallback_kills
            fallback_kills += 1

    def raise_timeout(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="taskkill", timeout=5)

    monkeypatch.setattr(process_manager_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr("core.tools.process_manager.subprocess.run", raise_timeout)

    ProcessManager._kill_process_tree(FakeProcess())  # type: ignore[arg-type]

    assert fallback_kills == 1


def test_windows_process_tree_kill_runs_taskkill_windowless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(process_manager_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        process_manager_module,
        "subprocess_creation_flags",
        lambda: expected_creation_flags,
    )
    monkeypatch.setattr("core.tools.process_manager.subprocess.run", successful_taskkill)

    ProcessManager._kill_process_tree(FakeProcess())  # type: ignore[arg-type]

    assert run_kwargs["creationflags"] == expected_creation_flags
    assert fallback_kills == 0


@pytest.mark.asyncio
async def test_log_file_holds_complete_output_beyond_buffer_cap(tmp_path: Path) -> None:
    # The in-memory buffer keeps only the newest bytes; the spool file must
    # still hold everything the process ever printed.
    manager = ProcessManager(
        buffer_cap_bytes=64,
        sweep_interval_seconds=3600,
        temporary_files=TemporaryFileManager(tmp_path),
    )
    try:
        process_id = await manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "print('start-marker'); print('x' * 500)"],
            env=None,
            cwd=None,
        )
        await poll_until_terminal(manager, process_id)

        tracked = manager.get_process(process_id, AGENT_A)
        assert tracked.truncated is True
        assert tracked.log_file is not None
        assert tracked.log_file.parent == tmp_path / "artifacts" / "temp" / "bash"

        content = tracked.log_file.read_text(encoding="utf-8")
        assert "start-marker" in content
        assert "x" * 500 in content
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_no_temporary_file_manager_means_no_log_file(manager: ProcessManager) -> None:
    process_id = await manager.spawn(
        SCOPE_A, AGENT_A, [sys.executable, "-c", "print('hi')"], env=None, cwd=None
    )
    await poll_until_terminal(manager, process_id)

    assert manager.get_process(process_id, AGENT_A).log_file is None


@pytest.mark.asyncio
async def test_log_file_is_stripped_of_ansi_escape_sequences(tmp_path: Path) -> None:
    manager = ProcessManager(
        sweep_interval_seconds=3600,
        temporary_files=TemporaryFileManager(tmp_path),
    )
    try:
        script = "import sys; esc = chr(27); sys.stdout.write(f'{esc}[31mred{esc}[0m done\n')"
        process_id = await manager.spawn(
            SCOPE_A, AGENT_A, [sys.executable, "-c", script], env=None, cwd=None
        )
        await poll_until_terminal(manager, process_id)

        tracked = manager.get_process(process_id, AGENT_A)
        assert tracked.log_file is not None
        content = tracked.log_file.read_text(encoding="utf-8")
        assert "red" in content and "done" in content
        assert "\x1b" not in content
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_log_lease_finishes_only_after_process_is_terminal(tmp_path: Path) -> None:
    temporary_files = TemporaryFileManager(
        tmp_path,
        retention={"bash": timedelta(milliseconds=1)},
    )
    manager = ProcessManager(sweep_interval_seconds=3600, temporary_files=temporary_files)
    try:
        process_id = await manager.spawn(
            SCOPE_A,
            AGENT_A,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=None,
            cwd=None,
        )
        tracked = manager.get_process(process_id, AGENT_A)
        assert tracked.log_file is not None
        time.sleep(0.01)

        temporary_files.sweep()
        assert tracked.log_file.exists(), "an active process log must survive cleanup"

        await manager.kill(process_id, AGENT_A)
        time.sleep(0.01)
        temporary_files.sweep()
        assert not tracked.log_file.exists()
    finally:
        await manager.aclose()


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

    def slow_taskkill(pid: int) -> bool:
        entered_taskkill.set()
        release_taskkill.wait(timeout=5)
        terminate_pid_forcibly(pid)
        return True

    monkeypatch.setattr(process_manager_module, "windows_taskkill_tree", slow_taskkill)

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

    def fake_taskkill(pid: int) -> bool:
        # Terminate the child for real so its wait task settles, but report
        # through the faked primitive under test.
        terminate_pid_forcibly(pid)
        return True

    monkeypatch.setattr(process_manager_module, "windows_taskkill_tree", fake_taskkill)

    await manager.cancel_scope_async(SCOPE_A)

    assert manager.get_process(scope_a_id, AGENT_A).status == "killed"
    assert manager.get_process(scope_b_id, AGENT_A).status == "running"


class TerminalNotificationRecorder:
    """Collects terminal notifications with an awaitable first-receipt event."""

    def __init__(self) -> None:
        self.notifications: list[dict[str, Any]] = []

    def __call__(self, notification: dict[str, Any]) -> None:
        self.notifications.append(notification)

    async def wait_for_notification(self, count: int = 1) -> None:
        for _ in range(200):
            if len(self.notifications) >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(
            f"expected {count} terminal notification(s), got {len(self.notifications)}"
        )


async def _await_terminal(manager: ProcessManager, process_id: str) -> None:
    """Let the wait task settle so the terminal notification has fired."""
    tracked = manager.get_process(process_id, AGENT_A)
    if tracked.wait_task is not None:
        await tracked.wait_task


@pytest.mark.asyncio
async def test_terminal_notification_fires_once_for_backgrounded_exit(
    manager: ProcessManager,
) -> None:
    recorder = TerminalNotificationRecorder()
    manager.add_terminal_callback(recorder)
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('background done')"],
        env=None,
        cwd=None,
    )
    manager.mark_backgrounded(process_id, AGENT_A)

    await _await_terminal(manager, process_id)
    await recorder.wait_for_notification()

    assert len(recorder.notifications) == 1
    notification = recorder.notifications[0]
    assert notification["process_id"] == process_id
    assert notification["agent_id"] == AGENT_A
    assert notification["status"] == "completed"
    assert notification["exit_code"] == 0
    assert notification["cancelled_by_user"] is False
    assert "background done" in notification["output"]
    assert notification["started_at"]
    assert notification["finished_at"]
    assert datetime.fromisoformat(notification["started_at"]) <= datetime.fromisoformat(
        notification["finished_at"]
    )


@pytest.mark.asyncio
async def test_terminal_notification_skips_foreground_processes(
    manager: ProcessManager,
) -> None:
    recorder = TerminalNotificationRecorder()
    manager.add_terminal_callback(recorder)
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('foreground done')"],
        env=None,
        cwd=None,
    )

    await _await_terminal(manager, process_id)
    await asyncio.sleep(0.05)

    assert recorder.notifications == []


@pytest.mark.asyncio
async def test_terminal_notification_fires_for_killed_process(
    manager: ProcessManager,
) -> None:
    recorder = TerminalNotificationRecorder()
    manager.add_terminal_callback(recorder)
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )
    manager.mark_backgrounded(process_id, AGENT_A)

    await manager.kill(process_id, AGENT_A)
    await _await_terminal(manager, process_id)
    await recorder.wait_for_notification()

    notification = recorder.notifications[0]
    assert notification["status"] == "killed"
    assert notification["finished_at"]


@pytest.mark.asyncio
async def test_terminal_notification_output_tail_is_capped() -> None:
    manager = ProcessManager(buffer_cap_bytes=PROCESS_BUFFER_CAP_BYTES, sweep_interval_seconds=3600)
    try:
        recorder = TerminalNotificationRecorder()
        manager.add_terminal_callback(recorder)
        script = "import sys; sys.stdout.write('x' * 40000); sys.stdout.flush()"
        process_id = await manager.spawn(
            SCOPE_A, AGENT_A, [sys.executable, "-c", script], env=None, cwd=None
        )
        manager.mark_backgrounded(process_id, AGENT_A)

        await _await_terminal(manager, process_id)
        await recorder.wait_for_notification()

        notification = recorder.notifications[0]
        assert len(notification["output"]) == PROCESS_TERMINAL_OUTPUT_CAP_CHARS
        assert notification["output"] == "x" * PROCESS_TERMINAL_OUTPUT_CAP_CHARS
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_terminal_notification_survives_a_failing_callback(
    manager: ProcessManager,
) -> None:
    def broken_callback(notification: dict[str, Any]) -> None:
        raise RuntimeError("bridge exploded")

    recorder = TerminalNotificationRecorder()
    manager.add_terminal_callback(broken_callback)
    manager.add_terminal_callback(recorder)
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('notified')"],
        env=None,
        cwd=None,
    )
    manager.mark_backgrounded(process_id, AGENT_A)

    await _await_terminal(manager, process_id)
    await recorder.wait_for_notification()

    assert recorder.notifications[0]["process_id"] == process_id


@pytest.mark.asyncio
async def test_terminal_callback_unsubscribe_stops_delivery(
    manager: ProcessManager,
) -> None:
    recorder = TerminalNotificationRecorder()
    unsubscribe = manager.add_terminal_callback(recorder)
    unsubscribe()
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('unsubscribed')"],
        env=None,
        cwd=None,
    )
    manager.mark_backgrounded(process_id, AGENT_A)

    await _await_terminal(manager, process_id)
    await asyncio.sleep(0.05)

    assert recorder.notifications == []
