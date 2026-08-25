"""Async background process management for shell-backed tools."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import ctypes
import os
import signal
import subprocess
import sys
import uuid
from asyncio import StreamWriter
from asyncio.subprocess import PIPE, Process
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.utils.ansi import strip_ansi
from core.utils.errors import VBotError
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.process_manager")

PROCESS_BUFFER_CAP_BYTES = 500 * 1024
FINISHED_PROCESS_TTL = timedelta(minutes=30)
SWEEP_INTERVAL_SECONDS = 60.0
INPUT_IDLE_SECONDS = 15.0
SUBMIT_BYTES = b"\r\n" if os.name == "nt" else b"\n"
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_WINDOWS_SERVER_JOB_HANDLE: int | None = None
_POSIX_LIFETIME_READ_FD: int | None = None
_POSIX_LIFETIME_WRITE_FD: int | None = None

ProcessStatus = Literal["running", "completed", "failed", "killed"]
OutputStreamName = Literal["stdout", "stderr"]


@dataclass(frozen=True, slots=True)
class GuardedProcessLaunch:
    """Exact argv plus inherited descriptors for a contained child launch."""

    argv: tuple[str, ...]
    pass_fds: tuple[int, ...] = ()


def activate_process_containment(*, platform_name: str = os.name) -> None:
    """Make this server process the OS-level lifetime owner of later children."""

    if platform_name == "nt":
        _activate_windows_process_job()
        return
    _activate_posix_lifetime_pipe()


def guarded_process_launch(
    argv: Sequence[str], *, platform_name: str = os.name
) -> GuardedProcessLaunch:
    """Wrap a POSIX child with the active server-lifetime guardian when enabled."""

    if not argv:
        raise ValueError("Process argv must not be empty")
    if platform_name == "nt" or _POSIX_LIFETIME_READ_FD is None:
        return GuardedProcessLaunch(tuple(argv))
    return GuardedProcessLaunch(
        (
            sys.executable,
            "-m",
            "core.tools.process_guardian",
            "--lifetime-fd",
            str(_POSIX_LIFETIME_READ_FD),
            "--",
            *argv,
        ),
        (_POSIX_LIFETIME_READ_FD,),
    )


def _activate_posix_lifetime_pipe() -> None:
    global _POSIX_LIFETIME_READ_FD, _POSIX_LIFETIME_WRITE_FD

    if _POSIX_LIFETIME_READ_FD is not None:
        return
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, False)
    os.set_inheritable(write_fd, False)
    _POSIX_LIFETIME_READ_FD = read_fd
    _POSIX_LIFETIME_WRITE_FD = write_fd


def _activate_windows_process_job() -> None:
    """Assign the server to a kill-on-close Windows Job Object."""

    global _WINDOWS_SERVER_JOB_HANDLE

    if _WINDOWS_SERVER_JOB_HANDLE is not None:
        return
    from ctypes import wintypes

    windows_ctypes = cast(Any, ctypes)

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise windows_ctypes.WinError(windows_ctypes.get_last_error())
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
    )
    if not kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = windows_ctypes.WinError(windows_ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = windows_ctypes.WinError(windows_ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    _WINDOWS_SERVER_JOB_HANDLE = int(job)


def subprocess_creation_flags(
    *,
    new_process_group: bool = False,
    breakaway: bool = False,
    platform_name: str = os.name,
) -> int:
    """Return platform flags for a windowless child process."""
    if platform_name != "nt":
        return 0

    flags = int(cast(Any, subprocess).CREATE_NO_WINDOW)
    if new_process_group:
        flags |= int(cast(Any, subprocess).CREATE_NEW_PROCESS_GROUP)
    if breakaway:
        flags |= int(cast(Any, subprocess).CREATE_BREAKAWAY_FROM_JOB)
    return flags


TASKKILL_TREE_TIMEOUT_SECONDS = 5


def windows_taskkill_tree(pid: int) -> bool:
    """Best-effort blocking ``taskkill`` of a whole Windows process tree.

    Returns True only when taskkill confirmed the tree was terminated. This
    call can block for up to ``TASKKILL_TREE_TIMEOUT_SECONDS``, so event-loop
    contexts must run it through :func:`kill_process_tree_async` (worker
    thread) instead of calling it directly.
    """
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TASKKILL_TREE_TIMEOUT_SECONDS,
            check=False,
            creationflags=subprocess_creation_flags(),
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


async def kill_process_tree_async(proc: Process) -> None:
    """Kill a whole process tree without blocking the event loop.

    Same contract as ``ProcessManager._kill_process_tree`` - including
    propagating ``ProcessLookupError`` on POSIX - but the Windows ``taskkill``
    subprocess runs in a worker thread so the loop never stalls behind it.
    """
    if os.name == "nt":
        killed = await asyncio.to_thread(windows_taskkill_tree, proc.pid)
        if not killed:
            _LOGGER.warning(
                "taskkill failed for pid=%s, falling back to direct kill",
                proc.pid,
            )
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        return

    _kill_process_tree_posix(proc)


def _kill_process_tree_posix(proc: Process) -> None:
    """Kill a POSIX process group; re-raises ProcessLookupError."""
    try:
        kill_process_group = cast(Any, os).__dict__["killpg"]
        kill_process_group(proc.pid, HARD_KILL_SIGNAL)
    except ProcessLookupError:
        raise
    except OSError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


class ProcessManagerError(VBotError):
    """Base class for expected process manager errors."""


class ProcessNotFoundError(ProcessManagerError):
    """Raised when a process is missing or belongs to another agent."""


class ProcessInputClosedError(ProcessManagerError):
    """Raised when writing to a process whose stdin is unavailable."""


class ProcessStillRunningError(ProcessManagerError):
    """Raised when an operation requires a finished process."""


@dataclass(frozen=True)
class OutputChunk:
    """One stdout or stderr byte chunk stored with absolute buffer offsets."""

    stream: OutputStreamName
    data: bytes
    start_offset: int
    end_offset: int


@dataclass
class TrackedProcess:
    """In-memory state for one managed process."""

    process_id: str
    agent_id: str
    scope_key: str
    proc: Process
    combined_buffer: bytearray
    truncated: bool
    stdout_lines: list[bytes]
    stderr_lines: list[bytes]
    foreground_stdout_bytes: int
    foreground_stderr_bytes: int
    status: ProcessStatus
    exit_code: int | None
    started_at: datetime
    finished_at: datetime | None
    last_poll_at: datetime | None
    last_output_at: datetime | None
    stdin_open: bool
    foreground_capture_open: bool = True
    buffer_start_offset: int = 0
    poll_offset: int = 0
    log_file: Path | None = None
    log_handle: TextIO | None = field(default=None, repr=False)
    log_decoder: codecs.IncrementalDecoder | None = field(default=None, repr=False)
    log_lease: TemporaryFileLease | None = field(default=None, repr=False)
    output_chunks: list[OutputChunk] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    output_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stdout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    stderr_task: asyncio.Task[None] | None = field(default=None, repr=False)
    wait_task: asyncio.Task[None] | None = field(default=None, repr=False)
    completion_notification_task: asyncio.Task[None] | None = field(default=None, repr=False)
    completion_acknowledged: bool = False
    cancelled_by_user: bool = False


class ProcessManager:
    """Spawn, track, poll, and terminate subprocesses."""

    def __init__(
        self,
        *,
        buffer_cap_bytes: int = PROCESS_BUFFER_CAP_BYTES,
        finished_process_ttl: timedelta = FINISHED_PROCESS_TTL,
        sweep_interval_seconds: float = SWEEP_INTERVAL_SECONDS,
        temporary_files: TemporaryFileManager | None = None,
    ) -> None:
        if buffer_cap_bytes < 1:
            raise ValueError("Process buffer cap must be at least 1 byte")
        if sweep_interval_seconds <= 0:
            raise ValueError("Sweep interval must be positive")

        self._buffer_cap_bytes = buffer_cap_bytes
        self._finished_process_ttl = finished_process_ttl
        self._sweep_interval_seconds = sweep_interval_seconds
        self._temporary_files = temporary_files
        self._processes: dict[str, TrackedProcess] = {}
        self._sweeper_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the TTL sweeper task."""
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return

        self._sweeper_task = asyncio.create_task(self._sweep_loop(), name="process-manager-sweep")

    def stop(self) -> None:
        """Stop the TTL sweeper task and kill active processes."""
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None

        for tracked in list(self._processes.values()):
            notification_task = tracked.completion_notification_task
            if notification_task is not None and not notification_task.done():
                notification_task.cancel()
            if tracked.status == "running":
                self._kill_process_now(tracked)

    async def aclose(self) -> None:
        """Stop the manager and await tracked task cleanup."""
        sweeper_task = self._sweeper_task
        self.stop()

        tasks: list[asyncio.Task[None]] = []
        if sweeper_task is not None and not sweeper_task.done():
            tasks.append(sweeper_task)
        for tracked in list(self._processes.values()):
            for task in (
                tracked.wait_task,
                tracked.stdout_task,
                tracked.stderr_task,
                tracked.completion_notification_task,
            ):
                if task is not None and not task.done():
                    tasks.append(task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def spawn(
        self,
        scope_key: str,
        agent_id: str,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None,
        cwd: str | Path | None,
    ) -> str:
        """Start a subprocess and return its process id."""
        if not scope_key:
            raise ValueError("Process scope key is required")
        if not agent_id:
            raise ValueError("Process agent id is required")
        if not argv:
            raise ValueError("Process argv must not be empty")

        process_env = os.environ.copy()
        if env is not None:
            process_env.update(env)
        process_env["PYTHONIOENCODING"] = "utf-8"

        launch = guarded_process_launch(argv)
        creationflags = subprocess_creation_flags(new_process_group=True)
        start_new_session = os.name != "nt"
        pass_fds = launch.pass_fds if os.name != "nt" else ()

        proc = await asyncio.create_subprocess_exec(
            *launch.argv,
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
            env=process_env,
            cwd=str(cwd) if cwd is not None else None,
            creationflags=creationflags,
            start_new_session=start_new_session,
            pass_fds=pass_fds,
        )
        process_id = uuid.uuid4().hex
        tracked = TrackedProcess(
            process_id=process_id,
            agent_id=agent_id,
            scope_key=scope_key,
            proc=proc,
            combined_buffer=bytearray(),
            truncated=False,
            stdout_lines=[],
            stderr_lines=[],
            foreground_stdout_bytes=0,
            foreground_stderr_bytes=0,
            status="running",
            exit_code=None,
            started_at=_utc_now(),
            finished_at=None,
            last_poll_at=None,
            last_output_at=None,
            stdin_open=proc.stdin is not None,
        )
        self._open_log_file(tracked)
        self._processes[process_id] = tracked
        tracked.stdout_task = asyncio.create_task(
            self._read_stream(tracked, "stdout"),
            name=f"process:{process_id}:stdout",
        )
        tracked.stdout_task.add_done_callback(
            lambda task: _log_background_task_result(
                task, f"Process stdout reader failed for process={process_id}"
            )
        )
        tracked.stderr_task = asyncio.create_task(
            self._read_stream(tracked, "stderr"),
            name=f"process:{process_id}:stderr",
        )
        tracked.stderr_task.add_done_callback(
            lambda task: _log_background_task_result(
                task, f"Process stderr reader failed for process={process_id}"
            )
        )
        tracked.wait_task = asyncio.create_task(
            self._watch_process(tracked),
            name=f"process:{process_id}:wait",
        )
        tracked.wait_task.add_done_callback(
            lambda task: _log_background_task_result(
                task, f"Process completion watcher failed for process={process_id}"
            )
        )
        return process_id

    def get_process(self, process_id: str, agent_id: str) -> TrackedProcess:
        """Return a tracked process owned by agent_id, hiding cross-agent processes."""
        return self._process_for_agent(process_id, agent_id)

    def list_processes(self, agent_id: str) -> list[TrackedProcess]:
        """Return processes visible to one agent."""
        return sorted(
            [tracked for tracked in self._processes.values() if tracked.agent_id == agent_id],
            key=lambda tracked: tracked.started_at,
        )

    async def poll(self, process_id: str, agent_id: str, timeout_ms: int = 0) -> dict[str, object]:
        """Return output produced since the previous poll for this process."""
        tracked = self._process_for_agent(process_id, agent_id)
        timeout_seconds = max(timeout_ms, 0) / 1000
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        while True:
            poll_result = await self._poll_once(tracked)
            if poll_result["output"] or tracked.status != "running" or timeout_seconds == 0:
                return poll_result

            remaining_seconds = deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                return poll_result

            tracked.output_event.clear()
            poll_result = await self._poll_once(tracked)
            if poll_result["output"] or tracked.status != "running":
                return poll_result

            try:
                await asyncio.wait_for(tracked.output_event.wait(), timeout=remaining_seconds)
            except TimeoutError:
                return await self._poll_once(tracked)

    async def log(
        self,
        process_id: str,
        agent_id: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Return a line window from the combined output buffer."""
        if offset < 0:
            raise ValueError("Log offset must not be negative")
        if limit is not None and limit < 0:
            raise ValueError("Log limit must not be negative")

        tracked = self._process_for_agent(process_id, agent_id)
        async with tracked.lock:
            text = _decode(bytes(tracked.combined_buffer))
            lines = text.splitlines(keepends=True)
            selected_lines = lines[offset:] if limit is None else lines[offset : offset + limit]
            return {
                "process_id": tracked.process_id,
                "output": "".join(selected_lines),
                "total_lines": len(lines),
                "truncated": tracked.truncated,
            }

    async def snapshot(self, process_id: str, agent_id: str) -> dict[str, object]:
        """Return one non-consuming snapshot of one tracked process."""
        tracked = self._process_for_agent(process_id, agent_id)
        async with tracked.lock:
            return {
                "process_id": tracked.process_id,
                "status": tracked.status,
                "exit_code": tracked.exit_code,
                "started_at": tracked.started_at,
                "finished_at": tracked.finished_at,
                "output": _decode(bytes(tracked.combined_buffer)),
                "truncated": tracked.truncated,
                "stdin_open": tracked.stdin_open,
                "waiting_for_input": _is_waiting_for_input(tracked),
                "log_file": tracked.log_file,
            }

    async def send_input(
        self,
        process_id: str,
        agent_id: str,
        text: str,
        *,
        newline: bool,
        eof: bool,
    ) -> None:
        """Send UTF-8 text, an optional line ending, and optional EOF to stdin."""
        tracked = self._process_for_agent(process_id, agent_id)
        stdin = tracked.proc.stdin
        if stdin is None or not tracked.stdin_open:
            raise ProcessInputClosedError(f"Process stdin is closed: {process_id}")

        payload = text.encode("utf-8")
        if newline:
            payload += SUBMIT_BYTES
        if payload:
            await self._write_stdin(tracked, stdin, payload)
        if eof:
            await self._close_stdin(tracked)

    @staticmethod
    async def _write_stdin(
        tracked: TrackedProcess,
        stdin: StreamWriter,
        payload: bytes,
    ) -> None:
        """Write bytes to stdin, mapping a raced close to ProcessInputClosedError.

        The upfront ``stdin_open`` check cannot stop a kill or the process
        exiting from closing stdin while ``drain()`` awaits, so a concurrent
        close surfaces here as a pipe error. Translate it to the expected
        closed-stdin error instead of leaking BrokenPipeError/
        ConnectionResetError to callers.
        """
        try:
            stdin.write(payload)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            tracked.stdin_open = False
            raise ProcessInputClosedError(
                f"Process stdin is closed: {tracked.process_id}"
            ) from error

    async def kill(self, process_id: str, agent_id: str) -> None:
        """Terminate a tracked process with SIGKILL / platform equivalent."""
        tracked = self._process_for_agent(process_id, agent_id)
        await self._kill_process(tracked)

    async def cancel_for_user(self, process_id: str, agent_id: str) -> TrackedProcess:
        """Terminate one running process and retain its explicit user origin."""
        tracked = self._process_for_agent(process_id, agent_id)
        await self._kill_process(tracked, cancelled_by_user=True)
        return tracked

    def mark_backgrounded(self, process_id: str, agent_id: str) -> None:
        """Stop accumulating foreground-only stdout/stderr line buffers."""
        tracked = self._process_for_agent(process_id, agent_id)
        tracked.foreground_capture_open = False

    def register_completion_notification(
        self,
        process_id: str,
        agent_id: str,
        task: asyncio.Task[None],
    ) -> None:
        """Track the automatic completion delivery for one background process."""
        tracked = self._process_for_agent(process_id, agent_id)
        current = tracked.completion_notification_task
        if current is not None and not current.done():
            raise ProcessManagerError(
                f"Process completion notification is already registered: {process_id}"
            )
        tracked.completion_notification_task = task
        if tracked.completion_acknowledged:
            task.cancel()

    def acknowledge_completion(self, process_id: str, agent_id: str) -> None:
        """Cancel automatic delivery after a terminal result was durably delivered."""
        tracked = self._process_for_agent(process_id, agent_id)
        if tracked.status == "running":
            raise ProcessStillRunningError(f"Process is still running: {process_id}")
        tracked.completion_acknowledged = True
        notification_task = tracked.completion_notification_task
        if notification_task is not None and not notification_task.done():
            notification_task.cancel()

    def cancel_scope(self, scope_key: str) -> None:
        """Kill active processes in a run scope synchronously.

        Prefer :meth:`cancel_scope_async` on the event loop - the Windows
        tree-kill can block for seconds. This variant is for shutdown paths.
        """
        if not scope_key:
            return

        for tracked in list(self._processes.values()):
            if tracked.scope_key == scope_key and tracked.status == "running":
                self._kill_process_now(tracked)

    async def sweep_finished(self) -> None:
        """Remove finished processes older than the configured TTL."""
        expires_before = _utc_now() - self._finished_process_ttl
        expired_ids = [
            tracked.process_id
            for tracked in self._processes.values()
            if tracked.finished_at is not None and tracked.finished_at < expires_before
        ]
        for process_id in expired_ids:
            self._processes.pop(process_id, None)

    def _open_log_file(self, tracked: TrackedProcess) -> None:
        """Attach an incremental spool file so the full output survives buffer caps.

        The in-memory buffer keeps only the newest ``buffer_cap_bytes``; the log
        file receives every chunk as it arrives, so it is the complete record a
        tool result can point the model at. Spooling is best-effort: on any I/O
        error the process simply runs without a log file.
        """
        if self._temporary_files is None:
            return

        lease: TemporaryFileLease | None = None
        try:
            lease = self._temporary_files.create("bash", ".log")
            # newline="" keeps the process's own line endings byte-faithful.
            tracked.log_handle = lease.path.open("w", encoding="utf-8", newline="")
        except OSError as error:
            if lease is not None:
                lease.finish()
            _LOGGER.warning(
                "Process log file unavailable for process=%s: %s",
                tracked.process_id,
                error,
            )
            return

        tracked.log_file = lease.path
        tracked.log_lease = lease
        # Chunks can split multi-byte UTF-8 characters; an incremental decoder
        # carries the partial bytes over to the next chunk instead of replacing.
        tracked.log_decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def _spill_to_log_file(self, tracked: TrackedProcess, chunk: bytes) -> None:
        if tracked.log_handle is None or tracked.log_decoder is None:
            return

        try:
            text = strip_ansi(tracked.log_decoder.decode(chunk))
            if text:
                tracked.log_handle.write(text)
                # Flush per chunk so the file is greppable while the process runs.
                tracked.log_handle.flush()
        except OSError as error:
            _LOGGER.warning(
                "Process log file write failed for process=%s, disabling: %s",
                tracked.process_id,
                error,
            )
            self._close_log_file(tracked)
            tracked.log_file = None

    def _close_log_file(self, tracked: TrackedProcess) -> None:
        if tracked.log_handle is not None:
            with contextlib.suppress(OSError):
                if tracked.log_decoder is not None:
                    remainder = strip_ansi(tracked.log_decoder.decode(b"", final=True))
                    if remainder:
                        tracked.log_handle.write(remainder)
                tracked.log_handle.close()
        tracked.log_handle = None
        tracked.log_decoder = None
        if tracked.log_lease is not None:
            tracked.log_lease.finish()
            tracked.log_lease = None

    async def _poll_once(self, tracked: TrackedProcess) -> dict[str, object]:
        async with tracked.lock:
            start_offset = max(tracked.poll_offset, tracked.buffer_start_offset)
            end_offset = tracked.buffer_start_offset + len(tracked.combined_buffer)
            relative_start = start_offset - tracked.buffer_start_offset
            output = bytes(tracked.combined_buffer[relative_start:])
            chunks = _chunks_between(tracked.output_chunks, start_offset, end_offset)
            tracked.poll_offset = end_offset
            tracked.last_poll_at = _utc_now()
            return {
                "process_id": tracked.process_id,
                "status": tracked.status,
                "exit_code": tracked.exit_code,
                "output": _decode(output),
                "stdout": _decode(
                    b"".join(chunk.data for chunk in chunks if chunk.stream == "stdout")
                ),
                "stderr": _decode(
                    b"".join(chunk.data for chunk in chunks if chunk.stream == "stderr")
                ),
                "chunks": [
                    {"stream": chunk.stream, "data": _decode(chunk.data)} for chunk in chunks
                ],
                "truncated": tracked.truncated,
                "waiting_for_input": _is_waiting_for_input(tracked),
            }

    async def _read_stream(self, tracked: TrackedProcess, stream_name: OutputStreamName) -> None:
        stream = tracked.proc.stdout if stream_name == "stdout" else tracked.proc.stderr
        if stream is None:
            return

        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            async with tracked.lock:
                self._append_output(tracked, stream_name, chunk)
            tracked.output_event.set()

    async def _watch_process(self, tracked: TrackedProcess) -> None:
        return_code = await tracked.proc.wait()
        await self._close_stdin(tracked)
        await self._await_reader_tasks(tracked)
        self._release_process_pipe_references(tracked)
        async with tracked.lock:
            tracked.exit_code = return_code
            if tracked.status == "running":
                tracked.status = "completed" if return_code == 0 else "failed"
            tracked.finished_at = _utc_now()
            tracked.stdin_open = False
            self._close_log_file(tracked)
        tracked.output_event.set()

    async def _await_reader_tasks(self, tracked: TrackedProcess) -> None:
        tasks = [task for task in (tracked.stdout_task, tracked.stderr_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _append_output(
        self,
        tracked: TrackedProcess,
        stream_name: OutputStreamName,
        chunk: bytes,
    ) -> None:
        start_offset = tracked.buffer_start_offset + len(tracked.combined_buffer)
        tracked.combined_buffer.extend(chunk)
        end_offset = start_offset + len(chunk)
        tracked.output_chunks.append(OutputChunk(stream_name, chunk, start_offset, end_offset))
        self._spill_to_log_file(tracked, chunk)
        if tracked.foreground_capture_open:
            target = tracked.stdout_lines if stream_name == "stdout" else tracked.stderr_lines
            target.append(chunk)
            if stream_name == "stdout":
                tracked.foreground_stdout_bytes += len(chunk)
            else:
                tracked.foreground_stderr_bytes += len(chunk)
            self._enforce_foreground_capture_cap(tracked, stream_name)
        tracked.last_output_at = _utc_now()
        self._enforce_buffer_cap(tracked)

    def _enforce_foreground_capture_cap(
        self,
        tracked: TrackedProcess,
        newest_stream_name: OutputStreamName,
    ) -> None:
        overflow = (
            tracked.foreground_stdout_bytes
            + tracked.foreground_stderr_bytes
            - self._buffer_cap_bytes
        )
        if overflow <= 0:
            return

        first_stream_name: OutputStreamName = (
            "stderr" if newest_stream_name == "stdout" else "stdout"
        )
        overflow = self._trim_foreground_stream(tracked, first_stream_name, overflow)
        if overflow > 0:
            self._trim_foreground_stream(tracked, newest_stream_name, overflow)
        tracked.truncated = True

    @staticmethod
    def _trim_foreground_stream(
        tracked: TrackedProcess,
        stream_name: OutputStreamName,
        bytes_to_remove: int,
    ) -> int:
        chunks = tracked.stdout_lines if stream_name == "stdout" else tracked.stderr_lines
        while bytes_to_remove > 0 and chunks:
            chunk = chunks[0]
            if len(chunk) <= bytes_to_remove:
                chunks.pop(0)
                bytes_to_remove -= len(chunk)
                removed = len(chunk)
            else:
                chunks[0] = chunk[bytes_to_remove:]
                removed = bytes_to_remove
                bytes_to_remove = 0

            if stream_name == "stdout":
                tracked.foreground_stdout_bytes -= removed
            else:
                tracked.foreground_stderr_bytes -= removed

        return bytes_to_remove

    def _enforce_buffer_cap(self, tracked: TrackedProcess) -> None:
        overflow = len(tracked.combined_buffer) - self._buffer_cap_bytes
        if overflow <= 0:
            return

        del tracked.combined_buffer[:overflow]
        tracked.buffer_start_offset += overflow
        tracked.truncated = True
        tracked.output_chunks = [
            chunk
            for chunk in tracked.output_chunks
            if chunk.end_offset > tracked.buffer_start_offset
        ]

    async def _kill_process(
        self,
        tracked: TrackedProcess,
        *,
        cancelled_by_user: bool = False,
    ) -> None:
        if tracked.status != "running":
            return

        self._begin_kill(tracked, cancelled_by_user=cancelled_by_user)
        try:
            await kill_process_tree_async(tracked.proc)
        except ProcessLookupError:
            self._finish_process_lookup_error(tracked)
        tracked.output_event.set()
        if tracked.wait_task is not None:
            await asyncio.gather(tracked.wait_task, return_exceptions=True)

    def _kill_process_now(
        self,
        tracked: TrackedProcess,
        *,
        cancelled_by_user: bool = False,
    ) -> None:
        """Kill synchronously; only for shutdown paths off the event loop."""
        if tracked.status != "running":
            return

        self._begin_kill(tracked, cancelled_by_user=cancelled_by_user)
        try:
            self._kill_process_tree(tracked.proc)
        except ProcessLookupError:
            self._finish_process_lookup_error(tracked)
        tracked.output_event.set()

    def _begin_kill(self, tracked: TrackedProcess, *, cancelled_by_user: bool) -> None:
        tracked.cancelled_by_user = cancelled_by_user
        tracked.status = "killed"
        self._close_stdin_now(tracked)

    @staticmethod
    def _finish_process_lookup_error(tracked: TrackedProcess) -> None:
        tracked.finished_at = _utc_now()
        tracked.stdin_open = False

    async def cancel_scope_async(self, scope_key: str) -> None:
        """Kill active processes in a run scope without blocking the loop."""
        if not scope_key:
            return

        for tracked in list(self._processes.values()):
            if tracked.scope_key == scope_key and tracked.status == "running":
                await self._kill_process(tracked)

    @staticmethod
    def _kill_process_tree(proc: Process) -> None:
        if os.name == "nt":
            if windows_taskkill_tree(proc.pid):
                return
            _LOGGER.warning(
                "taskkill failed for pid=%s, falling back to direct kill",
                proc.pid,
            )
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return

        _kill_process_tree_posix(proc)

    @classmethod
    async def _close_stdin(cls, tracked: TrackedProcess) -> None:
        cls._close_stdin_now(tracked)
        stdin = tracked.proc.stdin
        if stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
            await stdin.wait_closed()

    @staticmethod
    def _close_stdin_now(tracked: TrackedProcess) -> None:
        stdin = tracked.proc.stdin
        if stdin is None or not tracked.stdin_open:
            tracked.stdin_open = False
            return

        try:
            if stdin.can_write_eof():
                stdin.write_eof()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as error:
            _LOGGER.warning(
                "Process stdin EOF write failed for process=%s: %s",
                tracked.process_id,
                error,
            )
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
            stdin.close()
        tracked.stdin_open = False

    @staticmethod
    def _release_process_pipe_references(tracked: TrackedProcess) -> None:
        transport = getattr(tracked.proc, "_transport", None)
        pipes = getattr(transport, "_pipes", None)
        if not isinstance(pipes, dict):
            return
        pipes.clear()

    def _process_for_agent(self, process_id: str, agent_id: str) -> TrackedProcess:
        tracked = self._processes.get(process_id)
        if tracked is None or tracked.agent_id != agent_id:
            raise ProcessNotFoundError(f"Process not found: {process_id}")
        return tracked

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sweep_interval_seconds)
                await self.sweep_finished()
        except asyncio.CancelledError:
            return


def _log_background_task_result(task: asyncio.Task[Any], message: str) -> None:
    """Log an unexpected exception raised by a background task."""
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    _LOGGER.error(
        "%s: %s",
        message,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


def _chunks_between(
    chunks: Sequence[OutputChunk],
    start_offset: int,
    end_offset: int,
) -> list[OutputChunk]:
    selected_chunks: list[OutputChunk] = []
    for chunk in chunks:
        if chunk.end_offset <= start_offset or chunk.start_offset >= end_offset:
            continue
        chunk_start = max(start_offset, chunk.start_offset) - chunk.start_offset
        chunk_end = min(end_offset, chunk.end_offset) - chunk.start_offset
        selected_chunks.append(
            OutputChunk(
                stream=chunk.stream,
                data=chunk.data[chunk_start:chunk_end],
                start_offset=max(start_offset, chunk.start_offset),
                end_offset=min(end_offset, chunk.end_offset),
            )
        )
    return selected_chunks


def _decode(data: bytes) -> str:
    # Single decode chokepoint for all process output reaching the model and UI.
    # Raw bytes stay in the buffer for byte-accurate offset accounting; ANSI
    # control sequences are stripped only from the surfaced text, so a model
    # cannot copy escape codes into file writes and the output stays clean.
    return strip_ansi(data.decode("utf-8", errors="replace"))


def _is_waiting_for_input(tracked: TrackedProcess) -> bool:
    if not tracked.stdin_open:
        return False

    last_activity_at = tracked.last_output_at or tracked.started_at
    return (_utc_now() - last_activity_at).total_seconds() >= INPUT_IDLE_SECONDS


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "FINISHED_PROCESS_TTL",
    "GuardedProcessLaunch",
    "INPUT_IDLE_SECONDS",
    "PROCESS_BUFFER_CAP_BYTES",
    "ProcessManager",
    "ProcessManagerError",
    "TrackedProcess",
    "ProcessStatus",
    "ProcessInputClosedError",
    "ProcessNotFoundError",
    "ProcessStillRunningError",
    "activate_process_containment",
    "guarded_process_launch",
    "subprocess_creation_flags",
]
