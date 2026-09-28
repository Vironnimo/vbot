"""ProcessManager output: capture, decoding, spool files, and terminal notifications."""

from __future__ import annotations

import asyncio
import gc
import logging
import time
import weakref
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.storage import TemporaryFileManager
from core.tools.process_manager import PROCESS_TERMINAL_OUTPUT_CAP_CHARS, ProcessManager
from core.tools.tools import JsonObject
from tests.core.tools.process_manager_test_support import (
    AGENT_A,
    finish,
    spawn,
    stream_text,
)
from tests.core.tools.process_manager_test_support import (
    manager as manager,
)


def spool_manager(tmp_path: Path, **options: Any) -> ProcessManager:
    return ProcessManager(
        sweep_interval_seconds=3600, temporary_files=TemporaryFileManager(tmp_path), **options
    )


# --- capture -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_captures_stdout_and_stderr_as_separate_streams(manager) -> None:
    process_id = await spawn(
        manager, "import sys; print('hello'); print('problem', file=sys.stderr)"
    )

    result = await finish(manager, process_id)

    assert result["status"] == "completed"
    assert manager.get_process(process_id, AGENT_A).exit_code == 0
    assert stream_text(result, "stdout").strip() == "hello"
    assert stream_text(result, "stderr").strip() == "problem"


@pytest.mark.asyncio
async def test_stdin_is_closed_at_start_and_execution_resources_are_released_at_exit(
    manager,
) -> None:
    reads = "sys.stdin.read(), sys.stdin.readline(), sys.stdin.buffer.read()"
    process_id = await spawn(manager, f"import sys; assert not any(({reads})); print('eof')")
    tracked = manager.get_process(process_id, AGENT_A)
    assert tracked.proc is not None and tracked.proc.stdin is None
    process = weakref.ref(tracked.proc)
    transport = getattr(tracked.proc, "_transport", None)

    # Output no poll consumed before the exit stays readable afterwards, once.
    polled = await finish(manager, process_id)
    assert stream_text(polled, "stdout").strip() == "eof"
    assert (await manager.poll(process_id, AGENT_A))["chunks"] == []

    result = await manager.snapshot(process_id, AGENT_A)
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert str(result["output"]).strip() == "eof"
    assert "stdin_open" not in result
    assert "waiting_for_input" not in result
    assert str((await manager.log(process_id, AGENT_A))["output"]).strip() == "eof"
    # The finished process keeps its results, but not what only a running
    # process needs: the asyncio process, its pipes and readers become garbage.
    assert tracked.proc is None
    assert tracked.stdout_task is None and tracked.stderr_task is None
    assert not tracked.output_decoders and not tracked.output_chunks
    if transport is not None:
        assert transport.is_closing()
        assert getattr(transport, "_pipes", {}) == {}
    del transport
    gc.collect()
    assert process() is None


@pytest.mark.asyncio
async def test_output_and_log_file_are_stripped_of_ansi_escape_sequences(tmp_path) -> None:
    # A model must never see raw escape codes: it copies them into file writes.
    # The colored/title markers are removed while the visible text survives in
    # the streamed poll output, the full buffer, and the complete log file.
    script = (
        "import sys; esc = chr(27); bel = chr(7); "
        "sys.stdout.write(f'{esc}[31mred{esc}[0m and {esc}]0;title{bel}done\\n')"
    )
    manager = spool_manager(tmp_path)
    try:
        process_id = await spawn(manager, script)
        result = await finish(manager, process_id)
        log_result = await manager.log(process_id, AGENT_A)
        log_file = manager.get_process(process_id, AGENT_A).log_file
    finally:
        await manager.aclose()

    assert log_file is not None
    for surfaced in (
        stream_text(result, "stdout"),
        str(log_result["output"]),
        log_file.read_text(encoding="utf-8"),
    ):
        assert "red and done" in surfaced
        assert "\x1b" not in surfaced
        assert "[31m" not in surfaced
        assert "title" not in surfaced  # the OSC title payload is stripped too


@pytest.mark.asyncio
async def test_split_unicode_and_ansi_are_decoded_per_pipe_before_poll_and_spooling(
    tmp_path: Path,
) -> None:
    manager = spool_manager(tmp_path)
    try:
        process_id = await spawn(manager)
        tracked = manager.get_process(process_id, AGENT_A)
        await manager._capture_output(tracked, "stdout", b"\xe2")
        await manager._capture_output(tracked, "stderr", b"error \x1b[")
        first = await manager.poll(process_id, AGENT_A)
        assert first["chunks"] == [{"stream": "stderr", "data": "error "}]
        await manager._capture_output(tracked, "stdout", b"\x82\xac\x1b]title")
        await manager._capture_output(tracked, "stderr", b"31mred\x1b[0m")
        second = await manager.poll(process_id, AGENT_A)
        assert second["chunks"] == [
            {"stream": "stdout", "data": "€"},
            {"stream": "stderr", "data": "red"},
        ]
        await manager._capture_output(tracked, "stdout", b"\x1b")
        await manager._capture_output(tracked, "stderr", b"!")
        await manager._capture_output(tracked, "stdout", b"\\done")
        await manager.kill(process_id, AGENT_A)
        assert (await manager.snapshot(process_id, AGENT_A))["output"] == "error €red!done"
        assert tracked.log_file is not None
        assert tracked.log_file.read_text(encoding="utf-8") == "error €red!done"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "tasks", "message"),
    [
        ("_read_stream", ("stdout_task", "stderr_task"), "reader failed"),
        ("_watch_process", ("wait_task",), "completion watcher failed"),
    ],
    ids=["reader", "watcher"],
)
async def test_background_task_failure_is_logged(
    manager: ProcessManager,
    caplog: pytest.LogCaptureFixture,
    method: str,
    tasks: tuple[str, ...],
    message: str,
) -> None:
    async def boom(*_args: Any) -> None:
        raise RuntimeError("task exploded")

    setattr(manager, method, boom)

    with caplog.at_level(logging.ERROR, logger="vbot.tools.process_manager"):
        process_id = await spawn(manager)
        tracked = manager.get_process(process_id, AGENT_A)
        await asyncio.gather(*(getattr(tracked, name) for name in tasks), return_exceptions=True)
        await asyncio.sleep(0)
        await manager.kill(process_id, AGENT_A)

    errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and message in record.getMessage()
    ]
    assert errors, f"expected an error log containing {message!r}"
    assert errors[0].exc_info is not None


# --- spool files -------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_file_holds_complete_output_beyond_buffer_cap(tmp_path: Path) -> None:
    # The in-memory buffer keeps only the newest bytes; the spool file must
    # still hold everything the process ever printed.
    manager = spool_manager(tmp_path, buffer_cap_bytes=64)
    try:
        process_id = await spawn(manager, "print('start-marker'); print('x' * 500)")
        await finish(manager, process_id)
        tracked = manager.get_process(process_id, AGENT_A)
        buffered = await manager.log(process_id, AGENT_A)
    finally:
        await manager.aclose()

    assert tracked.truncated is True
    assert buffered["truncated"] is True
    assert isinstance(buffered["output"], str)
    assert len(buffered["output"]) == 64
    assert buffered["output"].rstrip() == "x" * len(buffered["output"].rstrip())
    assert tracked.log_file is not None
    assert tracked.log_file.parent == tmp_path / "artifacts" / "temp" / "bash"
    content = tracked.log_file.read_text(encoding="utf-8")
    assert "start-marker" in content
    assert "x" * 500 in content


@pytest.mark.asyncio
async def test_log_lease_finishes_only_after_process_is_terminal(tmp_path: Path) -> None:
    temporary_files = TemporaryFileManager(
        tmp_path,
        retention={"bash": timedelta(milliseconds=1)},
    )
    manager = ProcessManager(sweep_interval_seconds=3600, temporary_files=temporary_files)
    try:
        process_id = await spawn(manager)
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


# --- terminal notifications ----------------------------------------------------


@pytest.mark.asyncio
async def test_backgrounded_processes_notify_each_subscriber_once_when_terminal(
    manager: ProcessManager,
) -> None:
    notifications: list[JsonObject] = []
    unsubscribed: list[JsonObject] = []

    def broken_callback(notification: JsonObject) -> None:
        raise RuntimeError("bridge exploded")

    manager.add_terminal_callback(broken_callback)
    manager.add_terminal_callback(notifications.append)
    manager.add_terminal_callback(unsubscribed.append)()
    script = "import sys; sys.stdout.write('x' * 40000); sys.stdout.flush()"
    # Hand each process off before it can exit, as the shell Tool does.
    exited = await spawn(manager, script)
    manager.mark_backgrounded(exited, AGENT_A)
    killed = await spawn(manager)
    manager.mark_backgrounded(killed, AGENT_A)
    foreground = await spawn(manager, "print('foreground done')")

    await manager.kill(killed, AGENT_A)
    for process_id in (exited, foreground):
        await finish(manager, process_id)

    # Foreground processes never notify, and a failing callback blocks no other.
    by_id = {notification["process_id"]: notification for notification in notifications}
    assert len(notifications) == 2
    assert set(by_id) == {exited, killed}
    assert unsubscribed == []
    completed = by_id[exited]
    assert completed["agent_id"] == AGENT_A
    assert (completed["status"], completed["exit_code"]) == ("completed", 0)
    assert completed["cancelled_by_user"] is False
    # The accessor tail is capped independently of the in-memory buffer.
    assert completed["output"] == "x" * PROCESS_TERMINAL_OUTPUT_CAP_CHARS
    assert datetime.fromisoformat(completed["started_at"]) <= datetime.fromisoformat(
        completed["finished_at"]
    )
    assert by_id[killed]["status"] == "killed"
    assert by_id[killed]["finished_at"]
