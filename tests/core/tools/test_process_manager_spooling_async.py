"""Slow output files cannot block the loop or discard already captured output."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import pytest

import core.tools.process_manager as process_module
from core.storage import TemporaryFileManager
from core.tools._process_spool import ProcessOutputSpool
from core.tools.process_manager import ProcessManager, TrackedProcess


class PipeTransport(asyncio.ReadTransport):
    """Model closing a paused pipe: its finite buffer remains, then EOF arrives."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.paused = False
        self.pending = b""
        self.closed = asyncio.Event()
        reader.set_transport(self)

    def pause_reading(self) -> None:
        self.paused = True

    def resume_reading(self) -> None:
        if self.closed.is_set():
            return
        self.paused = False
        if self.pending:
            asyncio.get_running_loop().call_soon(self.reader.feed_data, self.pending)
            self.pending = b""

    def close(self) -> None:
        self.closed.set()
        asyncio.get_running_loop().call_soon(self.reader.feed_eof)


def make_tracked() -> tuple[TrackedProcess, dict[int, PipeTransport]]:
    stdout = asyncio.StreamReader(limit=64)
    stderr = asyncio.StreamReader(limit=64)
    pipes = {1: PipeTransport(stdout), 2: PipeTransport(stderr)}
    proc: Any = SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        returncode=None,
        _transport=SimpleNamespace(get_pipe_transport=pipes.get),
    )
    tracked = TrackedProcess(
        process_id="proc_test",
        agent_id="agent-test",
        project_id=None,
        scope_key="run-test",
        proc=proc,
        combined_buffer=bytearray(),
        truncated=False,
        stdout_lines=[],
        stderr_lines=[],
        foreground_stdout_bytes=0,
        foreground_stderr_bytes=0,
        status="running",
        exit_code=None,
        started_at=datetime.now(UTC),
        finished_at=None,
        last_poll_at=None,
    )
    return tracked, pipes


class BlockedFile:
    """Real-file wrapper whose first flush waits until the test releases it."""

    def __init__(self, handle: BinaryIO, loop: asyncio.AbstractEventLoop) -> None:
        self.handle = handle
        self.loop = loop
        self.loop_thread = threading.get_ident()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.blocked = False
        self.closed = False

    def write(self, chunk: bytes) -> int:
        assert threading.get_ident() != self.loop_thread
        return self.handle.write(chunk)

    def flush(self) -> None:
        assert threading.get_ident() != self.loop_thread
        if not self.blocked:
            self.blocked = True
            self.loop.call_soon_threadsafe(self.entered.set)
            if not self.release.wait(10):
                raise OSError("test did not release blocked file")
        self.handle.flush()

    def close(self) -> None:
        assert threading.get_ident() != self.loop_thread
        self.closed = True
        self.handle.close()


async def prepare(manager: ProcessManager, tracked: TrackedProcess) -> BlockedFile:
    await manager._open_log_file(tracked)
    assert tracked.spool is not None
    assert tracked.spool._handle is not None
    blocked = BlockedFile(tracked.spool._handle, asyncio.get_running_loop())
    tracked.spool._handle = blocked  # type: ignore[assignment]
    manager._processes[tracked.process_id] = tracked
    return blocked


@pytest.mark.asyncio
@pytest.mark.parametrize("waiter", ["natural", "kill", "shutdown"])
async def test_slow_spool_bounds_readers_and_drains_paused_pipes_after_exit(
    tmp_path: Path, monkeypatch, waiter: str
) -> None:
    temporary_files = TemporaryFileManager(tmp_path)
    manager = ProcessManager(temporary_files=temporary_files, buffer_cap_bytes=64)
    tracked, pipes = make_tracked()
    blocked = await prepare(manager, tracked)
    notifications: list[dict[str, Any]] = []
    manager.add_terminal_callback(notifications.append)
    tracked.backgrounded = True
    # The first write blocks, with the second pipe ready concurrently and more
    # bytes retained in paused StreamReaders. Neither reader may queue ahead.
    stdout = b"A" * 4096 + b"B" * 4096
    stderr = b"C" * 4096 + b"D" * 4096
    pipes[1].reader.feed_data(stdout)
    pipes[2].reader.feed_data(stderr)
    assert pipes[1].paused and pipes[2].paused
    pipes[1].pending = b"pending\xe2"
    tracked.stdout_task = asyncio.create_task(manager._read_stream(tracked, "stdout"))
    tracked.stderr_task = asyncio.create_task(manager._read_stream(tracked, "stderr"))
    monkeypatch.setattr(process_module, "PROCESS_OUTPUT_DRAIN_SECONDS", 0.01)
    # _release_process_pipe_references normally closes the real subprocess
    # transport as well; this fake already records its two pipe closes above.
    monkeypatch.setattr(manager, "_release_process_pipe_references", lambda _: None)
    finishing = None
    try:
        await asyncio.wait_for(blocked.entered.wait(), 2)
        # Snapshot acquisition proves the loop and tracked.lock remain usable
        # while the operating-system write is still blocked.
        snapshot = await asyncio.wait_for(manager.snapshot(tracked.process_id, tracked.agent_id), 1)
        assert snapshot["status"] == "running"
        assert tracked.buffer_start_offset + len(tracked.combined_buffer) == 8192
        # Inspect the retained pipe buffers without consuming them; asyncio's
        # public API deliberately has no queued-byte count.
        assert len(pipes[1].reader._buffer) == len(stdout) - 4096  # type: ignore[attr-defined]
        assert len(pipes[2].reader._buffer) == len(stderr) - 4096  # type: ignore[attr-defined]
        tracked.proc.returncode = 7  # type: ignore[misc]
        tracked.wait_task = asyncio.create_task(manager._watch_process(tracked))
        await asyncio.wait_for(pipes[1].closed.wait(), 2)
        await asyncio.wait_for(pipes[2].closed.wait(), 2)
        if waiter == "kill":
            finishing = asyncio.create_task(manager.kill(tracked.process_id, tracked.agent_id))
        elif waiter == "shutdown":
            manager.stop()
            finishing = asyncio.create_task(manager.aclose())
        else:
            finishing = tracked.wait_task
        await asyncio.sleep(0)
        assert not finishing.done()
        assert not tracked.wait_task.done()
        assert not notifications
        assert tracked.log_file in temporary_files._active
        blocked.release.set()
        await asyncio.wait_for(tracked.wait_task, 2)
        await asyncio.wait_for(finishing, 2)
        assert tracked.status == "failed" and tracked.exit_code == 7
        assert blocked.closed
        assert tracked.log_file not in temporary_files._active
        assert len(notifications) == 1
        assert tracked.log_file is not None
        content = tracked.log_file.read_bytes()
        # Interleaving is determined by capture order; each pipe stays ordered,
        # every buffered byte survives, and incomplete UTF-8 flushes once.
        assert content.startswith(b"A" * 4096 + b"C" * 4096)
        assert content.count(b"A") == content.count(b"B") == 4096
        assert content.count(b"C") == content.count(b"D") == 4096
        assert content.count("\ufffd".encode()) == 1
        assert b"pending" in content
        assert content.endswith(bytes(tracked.combined_buffer))
    finally:
        blocked.release.set()
        for pipe in pipes.values():
            if not pipe.closed.is_set():
                pipe.close()
        await asyncio.gather(tracked.stdout_task, tracked.stderr_task, return_exceptions=True)
        if tracked.wait_task is not None:
            await asyncio.gather(tracked.wait_task, return_exceptions=True)
        if finishing is not None:
            await asyncio.gather(finishing, return_exceptions=True)
        await manager._close_log_file(tracked)


@pytest.mark.asyncio
async def test_repeated_reader_cancellation_finishes_accepted_write_and_decoder(
    tmp_path: Path,
) -> None:
    manager = ProcessManager(temporary_files=TemporaryFileManager(tmp_path))
    tracked, pipes = make_tracked()
    blocked = await prepare(manager, tracked)
    pipes[1].reader.feed_data(b"kept\xe2")
    reader = asyncio.create_task(manager._read_stream(tracked, "stdout"))
    try:
        await asyncio.wait_for(blocked.entered.wait(), 2)
        reader.cancel()
        await asyncio.sleep(0)
        reader.cancel()
        await asyncio.sleep(0)
        assert not reader.done()
        assert not blocked.closed
        blocked.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reader, 2)
        await manager._close_log_file(tracked)
        assert tracked.log_file is not None
        assert (
            tracked.log_file.read_bytes() == bytes(tracked.combined_buffer) == b"kept\xef\xbf\xbd"
        )
    finally:
        blocked.release.set()
        await asyncio.gather(reader, return_exceptions=True)
        await manager._close_log_file(tracked)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["open", "write", "flush", "close"])
async def test_spool_io_failure_disables_file_without_losing_memory(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    manager = ProcessManager(temporary_files=TemporaryFileManager(tmp_path))
    tracked, _ = make_tracked()
    loop_thread = threading.get_ident()
    original_open = Path.open

    class FailingFile:
        def write(self, chunk: bytes) -> int:
            assert threading.get_ident() != loop_thread
            if failure == "write":
                raise OSError("disk unavailable")
            return len(chunk)

        def flush(self) -> None:
            assert threading.get_ident() != loop_thread
            if failure == "flush":
                raise OSError("disk unavailable")

        def close(self) -> None:
            assert threading.get_ident() != loop_thread
            if failure == "close":
                raise OSError("disk unavailable")

    def failing_open(path, *args, **kwargs):
        if args and args[0] == "wb":
            assert threading.get_ident() != loop_thread
            if failure == "open":
                raise OSError("disk unavailable")
            return FailingFile()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    await manager._open_log_file(tracked)
    await manager._capture_output(tracked, "stdout", b"still captured")
    await manager._close_log_file(tracked)
    assert tracked.log_file is None
    assert bytes(tracked.combined_buffer) == b"still captured"
    assert manager._temporary_files is not None
    assert not manager._temporary_files._active


@pytest.mark.asyncio
async def test_spool_open_and_close_including_lease_finish_run_off_loop(
    tmp_path: Path, monkeypatch
) -> None:
    temporary_files = TemporaryFileManager(tmp_path, retention={"bash": timedelta(seconds=1)})
    loop_thread = threading.get_ident()
    operations: list[str] = []
    for name in ("create", "_finish"):
        original = getattr(temporary_files, name)

        def check_thread(*args, _name=name, _original=original, **kwargs):
            assert threading.get_ident() != loop_thread
            operations.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(temporary_files, name, check_thread)
    spool = ProcessOutputSpool("proc_test", temporary_files)
    await spool.open()
    await spool.append(b"complete")
    await spool.close()
    assert operations == ["create", "_finish"]
    assert spool.path is not None
    assert spool.path.read_bytes() == b"complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["normal", "cancel", "shutdown"])
async def test_pending_log_open_reserves_id_and_settles_during_shutdown(
    tmp_path: Path, monkeypatch, finish: str
) -> None:
    manager = ProcessManager(temporary_files=TemporaryFileManager(tmp_path))
    loop = asyncio.get_running_loop()
    opening = asyncio.Event()
    release = threading.Event()
    original_open = ProcessOutputSpool._open
    candidates = iter(["proc_first", "proc_first", "proc_second"])

    def colliding_id(prefix, *, claim):
        assert prefix == "proc"
        for candidate in candidates:
            if claim(candidate):
                return candidate
        pytest.fail("allocation did not retain a distinct candidate")

    def blocked_open(spool):
        if spool._process_id == "proc_first":
            loop.call_soon_threadsafe(opening.set)
            if not release.wait(10):
                raise OSError("test did not release file open")
        original_open(spool)

    async def create_process(*args, **kwargs):
        tracked, pipes = make_tracked()
        for pipe in pipes.values():
            pipe.close()
        tracked.proc.returncode = 0  # type: ignore[misc]
        return tracked.proc

    monkeypatch.setattr(process_module, "new_id", colliding_id)
    monkeypatch.setattr(ProcessOutputSpool, "_open", blocked_open)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(manager, "_release_process_pipe_references", lambda _: None)
    first = asyncio.create_task(manager.spawn("run", "agent", ["fake"], env=None, cwd=None))
    shutdown = None
    try:
        await asyncio.wait_for(opening.wait(), 2)
        second_id = await asyncio.wait_for(
            manager.spawn("run", "agent", ["fake"], env=None, cwd=None), 2
        )
        assert second_id == "proc_second"
        assert not first.done()
        if finish == "shutdown":
            manager.stop()
            shutdown = asyncio.create_task(manager.aclose())
        elif finish == "cancel":
            first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        if shutdown is not None:
            assert not shutdown.done()
        release.set()
        if finish == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, 2)
        else:
            assert await asyncio.wait_for(first, 2) == "proc_first"
        if shutdown is not None:
            await asyncio.wait_for(shutdown, 2)
        await manager.aclose()
        assert not manager._pending_process_ids
        assert set(manager._processes) == {"proc_first", "proc_second"}
        assert all(process.finished_at is not None for process in manager._processes.values())
        assert manager._temporary_files is not None
        assert not manager._temporary_files._active
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        if shutdown is not None:
            await shutdown
        await manager.aclose()
