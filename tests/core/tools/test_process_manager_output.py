"""Process manager: output behavior."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from typing import Any

import pytest

from core.tools.process_manager import (
    PROCESS_BUFFER_CAP_BYTES,
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

AGENT_B = "agent-b"


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
@pytest.mark.parametrize(
    "read_expression", ["sys.stdin.read()", "sys.stdin.readline()", "sys.stdin.buffer.read()"]
)
async def test_spawn_provides_immediate_stdin_eof(manager, read_expression):
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", f"import sys; assert not {read_expression}; print('eof')"],
        env=None,
        cwd=None,
    )
    tracked = manager.get_process(process_id, AGENT_A)
    assert tracked.proc.stdin is None
    assert tracked.wait_task is not None
    await asyncio.wait_for(asyncio.shield(tracked.wait_task), 5)
    result = await manager.snapshot(process_id, AGENT_A)
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert str(result["output"]).strip() == "eof"
    assert "stdin_open" not in result
    assert "waiting_for_input" not in result


@pytest.mark.asyncio
async def test_completed_process_releases_pipe_references(manager: ProcessManager) -> None:
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
    assert tracked.proc.stdin is None
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
