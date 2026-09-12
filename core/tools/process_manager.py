"""Async background process management for shell-backed tools."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
from asyncio.subprocess import DEVNULL, PIPE, Process
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TextIO

from core.runs import RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.utils.ansi import strip_ansi
from core.utils.errors import VBotError
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.paths import model_path
from core.utils.processes import (
    guarded_process_launch,
    kill_process_tree,
    kill_process_tree_async,
    subprocess_creation_flags,
)

_LOGGER = get_logger("tools.process_manager")

PROCESS_BUFFER_CAP_BYTES = 500 * 1024
# Accessor-facing terminal notifications carry the same output scale the Tool
# result already shows, so the UI's final result mirrors the handed-off
# snapshot instead of growing with the (much larger) in-memory buffer.
PROCESS_TERMINAL_OUTPUT_CAP_CHARS = 30_000
FINISHED_PROCESS_TTL = timedelta(minutes=30)
SWEEP_INTERVAL_SECONDS = 60.0

ProcessStatus = Literal["running", "completed", "failed", "killed"]
OutputStreamName = Literal["stdout", "stderr"]


class ProcessManagerError(VBotError):
    """Base class for expected process manager errors."""


class ProcessNotFoundError(ProcessManagerError):
    """Raised when a process is missing or belongs to another agent."""


class ProcessStillRunningError(ProcessManagerError):
    """Raised when an operation requires a finished process."""


class ProcessTerminationError(ProcessManagerError):
    """A tree kill failed; the tracked process remains available for retry."""

    def __init__(self, process_id: str) -> None:
        super().__init__(
            f"Could not terminate process {process_id}. Its process tree may still be running. "
            "Retry the process Tool with action 'kill' and this process_id."
        )


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
    project_id: str | None
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
    execution_owner: RunExecutionOwner | None = None
    foreground_capture_open: bool = True
    buffer_start_offset: int = 0
    poll_offset: int = 0
    log_file: Path | None = None
    log_handle: TextIO | None = field(default=None, repr=False)
    log_decoder: codecs.IncrementalDecoder | None = field(default=None, repr=False)
    log_lease: TemporaryFileLease | None = field(default=None, repr=False)
    output_chunks: list[OutputChunk] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    kill_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    termination_failed: bool = False
    termination_targets: list[Any] = field(default_factory=list, repr=False)
    output_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stdout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    stderr_task: asyncio.Task[None] | None = field(default=None, repr=False)
    wait_task: asyncio.Task[None] | None = field(default=None, repr=False)
    completion_notification_task: asyncio.Task[None] | None = field(default=None, repr=False)
    completion_acknowledged: bool = False
    cancelled_by_user: bool = False
    backgrounded: bool = False
    terminal_notified: bool = False


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
        self._terminal_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._sweeper_task: asyncio.Task[None] | None = None
        self._closed_execution_groups: set[tuple[str, str, str]] = set()
        self._owned_spawns: dict[asyncio.Task[str], RunExecutionOwner] = {}

    def add_terminal_callback(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Subscribe to background-process terminal notifications.

        The callback receives one plain snapshot dict per handed-off process
        that reaches a terminal state. It runs synchronously on the event loop
        and must stay fast; the server's event bridge uses this seam to forward
        the notification to accessors.
        """
        self._terminal_callbacks.append(callback)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._terminal_callbacks.remove(callback)

        return unsubscribe

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

        failures: list[ProcessTerminationError] = []
        for tracked in list(self._processes.values()):
            notification_task = tracked.completion_notification_task
            if notification_task is not None and not notification_task.done():
                notification_task.cancel()
            if tracked.status == "running":
                try:
                    self._kill_process_now(tracked)
                except ProcessTerminationError as error:
                    failures.append(error)
        if failures:
            raise failures[0]

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
        project_id: str | None = None,
        env: dict[str, str] | None,
        cwd: str | Path | None,
        execution_owner: RunExecutionOwner | None = None,
    ) -> str:
        if execution_owner is None:
            return await self._spawn(
                scope_key, agent_id, argv, project_id=project_id, env=env, cwd=cwd
            )
        key = (execution_owner.extension, execution_owner.group_id, execution_owner.epoch)
        if key in self._closed_execution_groups:
            raise ProcessManagerError(
                "This Session is no longer available. Check its state through its Extension."
            )
        task = asyncio.create_task(
            self._spawn(
                scope_key,
                agent_id,
                argv,
                project_id=project_id,
                env=env,
                cwd=cwd,
                execution_owner=execution_owner,
            )
        )
        self._owned_spawns[task] = execution_owner
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:

            async def discard() -> None:
                try:
                    process_id = await task
                except Exception:
                    return
                await self._kill_process(self._processes[process_id])

            cleanup = asyncio.create_task(discard())
            while not cleanup.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
            cleanup.result()
            raise
        finally:
            self._owned_spawns.pop(task, None)

    async def _spawn(
        self,
        scope_key: str,
        agent_id: str,
        argv: Sequence[str],
        *,
        project_id: str | None = None,
        env: dict[str, str] | None,
        cwd: str | Path | None,
        execution_owner: RunExecutionOwner | None = None,
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
            stdin=DEVNULL,
            stdout=PIPE,
            stderr=PIPE,
            env=process_env,
            cwd=str(cwd) if cwd is not None else None,
            creationflags=creationflags,
            start_new_session=start_new_session,
            pass_fds=pass_fds,
        )
        process_id = new_id("proc", claim=lambda candidate: candidate not in self._processes)
        tracked = TrackedProcess(
            process_id=process_id,
            agent_id=agent_id,
            project_id=project_id,
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
            execution_owner=execution_owner,
        )
        self._open_log_file(tracked)
        self._processes[process_id] = tracked
        tracked.stdout_task = asyncio.create_task(
            self._read_stream(tracked, "stdout"),
            name=f"process:{process_id}:stdout",
        )
        tracked.stdout_task.add_done_callback(
            lambda task: log_background_task_result(
                task, f"Process stdout reader failed for process={process_id}"
            )
        )
        tracked.stderr_task = asyncio.create_task(
            self._read_stream(tracked, "stderr"),
            name=f"process:{process_id}:stderr",
        )
        tracked.stderr_task.add_done_callback(
            lambda task: log_background_task_result(
                task, f"Process stderr reader failed for process={process_id}"
            )
        )
        tracked.wait_task = asyncio.create_task(
            self._watch_process(tracked),
            name=f"process:{process_id}:wait",
        )
        tracked.wait_task.add_done_callback(
            lambda task: log_background_task_result(
                task, f"Process completion watcher failed for process={process_id}"
            )
        )
        return process_id

    def get_process(
        self, process_id: str, agent_id: str, *, project_id: str | None = None
    ) -> TrackedProcess:
        """Return a tracked process owned by one complete Agent address."""
        return self._process_for_agent(process_id, agent_id, project_id=project_id)

    def list_processes(
        self, agent_id: str, *, project_id: str | None = None
    ) -> list[TrackedProcess]:
        """Return processes visible to one complete Agent address."""
        return sorted(
            [
                tracked
                for tracked in self._processes.values()
                if tracked.agent_id == agent_id and tracked.project_id == project_id
            ],
            key=lambda tracked: tracked.started_at,
        )

    async def poll(
        self,
        process_id: str,
        agent_id: str,
        timeout_ms: int = 0,
        *,
        project_id: str | None = None,
    ) -> dict[str, object]:
        """Return output produced since the previous poll for this process."""
        tracked = self._process_for_agent(process_id, agent_id, project_id=project_id)
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
        *,
        project_id: str | None = None,
    ) -> dict[str, object]:
        """Return a line window from the combined output buffer."""
        if offset < 0:
            raise ValueError("Log offset must not be negative")
        if limit is not None and limit < 0:
            raise ValueError("Log limit must not be negative")

        tracked = self._process_for_agent(process_id, agent_id, project_id=project_id)
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

    async def snapshot(
        self, process_id: str, agent_id: str, *, project_id: str | None = None
    ) -> dict[str, object]:
        """Return one non-consuming snapshot of one tracked process."""
        tracked = self._process_for_agent(process_id, agent_id, project_id=project_id)
        async with tracked.lock:
            return {
                "process_id": tracked.process_id,
                "status": tracked.status,
                "exit_code": tracked.exit_code,
                "started_at": tracked.started_at,
                "finished_at": tracked.finished_at,
                "output": _decode(bytes(tracked.combined_buffer)),
                "truncated": tracked.truncated,
                "log_file": tracked.log_file,
            }

    async def kill(self, process_id: str, agent_id: str, *, project_id: str | None = None) -> None:
        """Terminate a tracked process with SIGKILL / platform equivalent."""
        tracked = self._process_for_agent(process_id, agent_id, project_id=project_id)
        await self._kill_process(tracked)

    async def cancel_for_user(
        self, process_id: str, agent_id: str, *, project_id: str | None = None
    ) -> TrackedProcess:
        """Terminate one running process and retain its explicit user origin."""
        tracked = self._process_for_agent(process_id, agent_id, project_id=project_id)
        await self._kill_process(tracked, cancelled_by_user=True)
        return tracked

    def mark_backgrounded(
        self, process_id: str, agent_id: str, *, project_id: str | None = None
    ) -> None:
        """Stop accumulating foreground-only stdout/stderr line buffers."""
        tracked = self._process_for_agent(process_id, agent_id, project_id=project_id)
        tracked.backgrounded = True
        tracked.foreground_capture_open = False

    def register_completion_notification(
        self,
        process_id: str,
        agent_id: str,
        task: asyncio.Task[None],
        *,
        project_id: str | None = None,
    ) -> None:
        """Track the automatic completion delivery for one background process."""
        tracked = self._process_for_agent(process_id, agent_id, project_id=project_id)
        current = tracked.completion_notification_task
        if current is not None and not current.done():
            raise ProcessManagerError(
                f"Process completion notification is already registered: {process_id}"
            )
        tracked.completion_notification_task = task
        if tracked.completion_acknowledged:
            task.cancel()

    def acknowledge_completion(
        self, process_id: str, agent_id: str, *, project_id: str | None = None
    ) -> None:
        """Cancel automatic delivery after a terminal result was durably delivered."""
        tracked = self._process_for_agent(process_id, agent_id, project_id=project_id)
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

        failures: list[ProcessTerminationError] = []
        for tracked in list(self._processes.values()):
            if tracked.scope_key == scope_key and tracked.status == "running":
                try:
                    self._kill_process_now(tracked)
                except ProcessTerminationError as error:
                    failures.append(error)
        if failures:
            raise failures[0]

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
        await self._await_reader_tasks(tracked)
        self._release_process_pipe_references(tracked)
        async with tracked.kill_lock, tracked.lock:
            tracked.exit_code = return_code
            if tracked.termination_failed:
                return
            if tracked.status == "running":
                tracked.status = "completed" if return_code == 0 else "failed"
            tracked.finished_at = _utc_now()
            self._close_log_file(tracked)
        tracked.output_event.set()
        self._notify_terminal(tracked)

    def _notify_terminal(self, tracked: TrackedProcess) -> None:
        """Publish one terminal notification for a handed-off process."""
        if not tracked.backgrounded or tracked.terminal_notified:
            return
        tracked.terminal_notified = True
        notification = self._terminal_notification(tracked)
        for callback in list(self._terminal_callbacks):
            try:
                callback(notification)
            except Exception:
                _LOGGER.error(
                    "Process terminal notification callback failed for process=%s",
                    tracked.process_id,
                )

    def _terminal_notification(self, tracked: TrackedProcess) -> dict[str, Any]:
        output = _decode(bytes(tracked.combined_buffer))
        if len(output) > PROCESS_TERMINAL_OUTPUT_CAP_CHARS:
            output = output[-PROCESS_TERMINAL_OUTPUT_CAP_CHARS:]
        return {
            "process_id": tracked.process_id,
            "agent_id": tracked.agent_id,
            "project_id": tracked.project_id,
            "status": tracked.status,
            "exit_code": tracked.exit_code,
            "cancelled_by_user": tracked.cancelled_by_user,
            "started_at": tracked.started_at.isoformat(),
            "finished_at": (tracked.finished_at.isoformat() if tracked.finished_at else None),
            "output": output,
            "truncated": tracked.truncated,
            "log_file": (model_path(tracked.log_file) if tracked.log_file is not None else None),
        }

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
        async with tracked.kill_lock:
            if tracked.status != "running":
                return
            if tracked.proc.returncode is None or tracked.termination_failed:
                try:
                    await kill_process_tree_async(tracked.proc, targets=tracked.termination_targets)
                except ProcessLookupError:
                    if tracked.termination_failed:
                        self._begin_kill(tracked, cancelled_by_user=cancelled_by_user)
                except OSError as error:
                    self._kill_failed(tracked, error)
                else:
                    self._begin_kill(tracked, cancelled_by_user=cancelled_by_user)
        tracked.output_event.set()
        if tracked.wait_task is not None:
            await asyncio.gather(tracked.wait_task, return_exceptions=True)
        if tracked.status == "killed" and tracked.finished_at is None:
            tracked.finished_at = _utc_now()
            self._close_log_file(tracked)
            self._notify_terminal(tracked)

    def _kill_process_now(
        self,
        tracked: TrackedProcess,
        *,
        cancelled_by_user: bool = False,
    ) -> None:
        """Kill synchronously; only for shutdown paths off the event loop."""
        if tracked.status != "running":
            return

        if tracked.proc.returncode is not None and not tracked.termination_failed:
            return
        try:
            self._kill_process_tree(tracked.proc, targets=tracked.termination_targets)
        except ProcessLookupError:
            if tracked.termination_failed:
                self._begin_kill(tracked, cancelled_by_user=cancelled_by_user)
        except OSError as error:
            self._kill_failed(tracked, error)
        else:
            self._begin_kill(tracked, cancelled_by_user=cancelled_by_user)
        tracked.output_event.set()
        if (
            tracked.status == "killed"
            and tracked.wait_task is not None
            and tracked.wait_task.done()
        ):
            tracked.finished_at = _utc_now()
            self._close_log_file(tracked)
            self._notify_terminal(tracked)

    def _begin_kill(self, tracked: TrackedProcess, *, cancelled_by_user: bool) -> None:
        tracked.cancelled_by_user = cancelled_by_user
        tracked.termination_failed = False
        tracked.status = "killed"

    @staticmethod
    def _kill_failed(tracked: TrackedProcess, error: OSError) -> None:
        tracked.termination_failed = True
        _LOGGER.warning("Process tree kill failed for process=%s: %s", tracked.process_id, error)
        tracked.output_event.set()
        raise ProcessTerminationError(tracked.process_id) from error

    def has_execution_work(self, owner: RunExecutionOwner) -> bool:
        return any(value == owner for value in self._owned_spawns.values()) or any(
            tracked.execution_owner == owner and tracked.status == "running"
            for tracked in self._processes.values()
        )

    async def close_execution_group(self, extension: str, group_id: str, epoch: str) -> None:
        """Close process admission and drain exactly this execution group's resources."""
        key = (extension, group_id, epoch)
        self._closed_execution_groups.add(key)
        pending = [
            task
            for task, owner in self._owned_spawns.items()
            if (owner.extension, owner.group_id, owner.epoch) == key
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        failures: list[ProcessTerminationError] = []
        for tracked in list(self._processes.values()):
            owner = tracked.execution_owner
            if owner is not None and (owner.extension, owner.group_id, owner.epoch) == key:
                watcher = tracked.completion_notification_task
                if watcher is not None:
                    watcher.cancel()
                try:
                    await self._kill_process(tracked)
                except ProcessTerminationError as error:
                    failures.append(error)
                if watcher is not None:
                    await asyncio.gather(watcher, return_exceptions=True)
        if failures:
            raise failures[0]

    async def cancel_scope_async(self, scope_key: str) -> None:
        """Kill active processes in a run scope without blocking the loop."""
        if not scope_key:
            return

        failures: list[ProcessTerminationError] = []
        for tracked in list(self._processes.values()):
            if tracked.scope_key == scope_key and tracked.status == "running":
                try:
                    await self._kill_process(tracked)
                except ProcessTerminationError as error:
                    failures.append(error)
        if failures:
            raise failures[0]

    @staticmethod
    def _kill_process_tree(proc: Process, *, targets: list[Any] | None = None) -> None:
        kill_process_tree(proc, targets=targets)

    @staticmethod
    def _release_process_pipe_references(tracked: TrackedProcess) -> None:
        transport = getattr(tracked.proc, "_transport", None)
        pipes = getattr(transport, "_pipes", None)
        if not isinstance(pipes, dict):
            return
        pipes.clear()

    def _process_for_agent(
        self, process_id: str, agent_id: str, *, project_id: str | None = None
    ) -> TrackedProcess:
        tracked = self._processes.get(process_id)
        if tracked is None or tracked.agent_id != agent_id or tracked.project_id != project_id:
            raise ProcessNotFoundError(f"Process not found: {process_id}")
        return tracked

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sweep_interval_seconds)
                await self.sweep_finished()
        except asyncio.CancelledError:
            return


def log_background_task_result(task: asyncio.Task[Any], message: str) -> None:
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "FINISHED_PROCESS_TTL",
    "PROCESS_BUFFER_CAP_BYTES",
    "ProcessManager",
    "ProcessManagerError",
    "TrackedProcess",
    "ProcessStatus",
    "ProcessNotFoundError",
    "ProcessStillRunningError",
    "log_background_task_result",
]
