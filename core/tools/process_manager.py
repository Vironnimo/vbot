"""Async background process management for shell-backed tools."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import uuid
from asyncio.subprocess import PIPE, Process
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from core.utils.errors import VBotError

PROCESS_BUFFER_CAP_BYTES = 500 * 1024
FINISHED_SESSION_TTL = timedelta(minutes=30)
SWEEP_INTERVAL_SECONDS = 60.0
INPUT_IDLE_SECONDS = 15.0
SUBMIT_BYTES = b"\r\n"
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)

ProcessStatus = Literal["running", "completed", "failed", "killed"]
OutputStreamName = Literal["stdout", "stderr"]


class ProcessManagerError(VBotError):
    """Base class for expected process manager errors."""


class SessionNotFoundError(ProcessManagerError):
    """Raised when a process session is missing or belongs to another agent."""


class SessionInputClosedError(ProcessManagerError):
    """Raised when writing to a process whose stdin is unavailable."""


class SessionStillRunningError(ProcessManagerError):
    """Raised when an operation requires a finished process session."""


@dataclass(frozen=True)
class OutputChunk:
    """One stdout or stderr byte chunk stored with absolute buffer offsets."""

    stream: OutputStreamName
    data: bytes
    start_offset: int
    end_offset: int


@dataclass
class ProcessSession:
    """In-memory state for one managed process."""

    session_id: str
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
    output_chunks: list[OutputChunk] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    output_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stdout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    stderr_task: asyncio.Task[None] | None = field(default=None, repr=False)
    wait_task: asyncio.Task[None] | None = field(default=None, repr=False)


class ProcessManager:
    """Spawn, track, poll, and terminate subprocess sessions."""

    def __init__(
        self,
        *,
        buffer_cap_bytes: int = PROCESS_BUFFER_CAP_BYTES,
        finished_session_ttl: timedelta = FINISHED_SESSION_TTL,
        sweep_interval_seconds: float = SWEEP_INTERVAL_SECONDS,
    ) -> None:
        if buffer_cap_bytes < 1:
            raise ValueError("Process buffer cap must be at least 1 byte")
        if sweep_interval_seconds <= 0:
            raise ValueError("Sweep interval must be positive")

        self._buffer_cap_bytes = buffer_cap_bytes
        self._finished_session_ttl = finished_session_ttl
        self._sweep_interval_seconds = sweep_interval_seconds
        self._sessions: dict[str, ProcessSession] = {}
        self._sweeper_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the TTL sweeper task."""
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return

        self._sweeper_task = asyncio.create_task(self._sweep_loop(), name="process-manager-sweep")

    def stop(self) -> None:
        """Stop the TTL sweeper task and kill active process sessions."""
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None

        for session in list(self._sessions.values()):
            if session.status == "running":
                self._kill_session_now(session)

    async def aclose(self) -> None:
        """Stop the manager and await tracked task cleanup."""
        sweeper_task = self._sweeper_task
        self.stop()

        tasks: list[asyncio.Task[None]] = []
        if sweeper_task is not None and not sweeper_task.done():
            tasks.append(sweeper_task)
        for session in list(self._sessions.values()):
            for task in (session.wait_task, session.stdout_task, session.stderr_task):
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
        """Start a subprocess and return its process session id."""
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

        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            start_new_session = False
        else:
            creationflags = 0
            start_new_session = True

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
            env=process_env,
            cwd=str(cwd) if cwd is not None else None,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        session_id = uuid.uuid4().hex
        session = ProcessSession(
            session_id=session_id,
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
        self._sessions[session_id] = session
        session.stdout_task = asyncio.create_task(
            self._read_stream(session, "stdout"),
            name=f"process:{session_id}:stdout",
        )
        session.stderr_task = asyncio.create_task(
            self._read_stream(session, "stderr"),
            name=f"process:{session_id}:stderr",
        )
        session.wait_task = asyncio.create_task(
            self._watch_process(session),
            name=f"process:{session_id}:wait",
        )
        return session_id

    def get_session(self, session_id: str, agent_id: str) -> ProcessSession:
        """Return a session owned by agent_id, hiding cross-agent sessions."""
        return self._session_for_agent(session_id, agent_id)

    def list_sessions(self, agent_id: str) -> list[ProcessSession]:
        """Return sessions visible to one agent."""
        return sorted(
            [session for session in self._sessions.values() if session.agent_id == agent_id],
            key=lambda session: session.started_at,
        )

    async def poll(self, session_id: str, agent_id: str, timeout_ms: int = 0) -> dict[str, object]:
        """Return output produced since the previous poll for this session."""
        session = self._session_for_agent(session_id, agent_id)
        timeout_seconds = max(timeout_ms, 0) / 1000
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        while True:
            poll_result = await self._poll_once(session)
            if poll_result["output"] or session.status != "running" or timeout_seconds == 0:
                return poll_result

            remaining_seconds = deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                return poll_result

            session.output_event.clear()
            poll_result = await self._poll_once(session)
            if poll_result["output"] or session.status != "running":
                return poll_result

            try:
                await asyncio.wait_for(session.output_event.wait(), timeout=remaining_seconds)
            except TimeoutError:
                return await self._poll_once(session)

    async def log(
        self,
        session_id: str,
        agent_id: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Return a line window from the combined output buffer."""
        if offset < 0:
            raise ValueError("Log offset must not be negative")
        if limit is not None and limit < 0:
            raise ValueError("Log limit must not be negative")

        session = self._session_for_agent(session_id, agent_id)
        async with session.lock:
            text = _decode(bytes(session.combined_buffer))
            lines = text.splitlines(keepends=True)
            selected_lines = lines[offset:] if limit is None else lines[offset : offset + limit]
            return {
                "session_id": session.session_id,
                "output": "".join(selected_lines),
                "total_lines": len(lines),
                "truncated": session.truncated,
            }

    async def write(
        self,
        session_id: str,
        agent_id: str,
        data: str,
        eof: bool = False,
    ) -> None:
        """Write UTF-8 text to process stdin and optionally close it."""
        session = self._session_for_agent(session_id, agent_id)
        stdin = session.proc.stdin
        if stdin is None or not session.stdin_open:
            raise SessionInputClosedError(f"Process stdin is closed: {session_id}")

        if data:
            stdin.write(data.encode("utf-8"))
            await stdin.drain()
        if eof:
            self._close_stdin(session)

    async def submit(self, session_id: str, agent_id: str) -> None:
        """Submit the current stdin line with CRLF."""
        session = self._session_for_agent(session_id, agent_id)
        stdin = session.proc.stdin
        if stdin is None or not session.stdin_open:
            raise SessionInputClosedError(f"Process stdin is closed: {session_id}")

        stdin.write(SUBMIT_BYTES)
        await stdin.drain()

    async def kill(self, session_id: str, agent_id: str) -> None:
        """Terminate a session with SIGKILL / platform equivalent."""
        session = self._session_for_agent(session_id, agent_id)
        await self._kill_session(session)

    async def clear(self, session_id: str, agent_id: str) -> None:
        """Remove a finished session from memory."""
        session = self._session_for_agent(session_id, agent_id)
        if session.status == "running":
            raise SessionStillRunningError(f"Process session is still running: {session_id}")

        self._sessions.pop(session_id, None)

    def mark_backgrounded(self, session_id: str, agent_id: str) -> None:
        """Stop accumulating foreground-only stdout/stderr line buffers."""
        session = self._session_for_agent(session_id, agent_id)
        session.foreground_capture_open = False

    def cancel_scope(self, scope_key: str) -> None:
        """Kill active sessions in a run scope, independent of agent ownership."""
        if not scope_key:
            return

        for session in list(self._sessions.values()):
            if session.scope_key == scope_key and session.status == "running":
                self._kill_session_now(session)

    async def sweep_finished(self) -> None:
        """Remove finished sessions older than the configured TTL."""
        expires_before = _utc_now() - self._finished_session_ttl
        expired_ids = [
            session.session_id
            for session in self._sessions.values()
            if session.finished_at is not None and session.finished_at < expires_before
        ]
        for session_id in expired_ids:
            self._sessions.pop(session_id, None)

    async def _poll_once(self, session: ProcessSession) -> dict[str, object]:
        async with session.lock:
            start_offset = max(session.poll_offset, session.buffer_start_offset)
            end_offset = session.buffer_start_offset + len(session.combined_buffer)
            relative_start = start_offset - session.buffer_start_offset
            output = bytes(session.combined_buffer[relative_start:])
            chunks = _chunks_between(session.output_chunks, start_offset, end_offset)
            session.poll_offset = end_offset
            session.last_poll_at = _utc_now()
            return {
                "session_id": session.session_id,
                "status": session.status,
                "exit_code": session.exit_code,
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
                "truncated": session.truncated,
                "waiting_for_input": _is_waiting_for_input(session),
            }

    async def _read_stream(self, session: ProcessSession, stream_name: OutputStreamName) -> None:
        stream = session.proc.stdout if stream_name == "stdout" else session.proc.stderr
        if stream is None:
            return

        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            async with session.lock:
                self._append_output(session, stream_name, chunk)
            session.output_event.set()

    async def _watch_process(self, session: ProcessSession) -> None:
        return_code = await session.proc.wait()
        await self._await_reader_tasks(session)
        async with session.lock:
            session.exit_code = return_code
            if session.status == "running":
                session.status = "completed" if return_code == 0 else "failed"
            session.finished_at = _utc_now()
            session.stdin_open = False
        session.output_event.set()

    async def _await_reader_tasks(self, session: ProcessSession) -> None:
        tasks = [task for task in (session.stdout_task, session.stderr_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _append_output(
        self,
        session: ProcessSession,
        stream_name: OutputStreamName,
        chunk: bytes,
    ) -> None:
        start_offset = session.buffer_start_offset + len(session.combined_buffer)
        session.combined_buffer.extend(chunk)
        end_offset = start_offset + len(chunk)
        session.output_chunks.append(OutputChunk(stream_name, chunk, start_offset, end_offset))
        if session.foreground_capture_open:
            target = session.stdout_lines if stream_name == "stdout" else session.stderr_lines
            target.append(chunk)
            if stream_name == "stdout":
                session.foreground_stdout_bytes += len(chunk)
            else:
                session.foreground_stderr_bytes += len(chunk)
            self._enforce_foreground_capture_cap(session, stream_name)
        session.last_output_at = _utc_now()
        self._enforce_buffer_cap(session)

    def _enforce_foreground_capture_cap(
        self,
        session: ProcessSession,
        newest_stream_name: OutputStreamName,
    ) -> None:
        overflow = (
            session.foreground_stdout_bytes
            + session.foreground_stderr_bytes
            - self._buffer_cap_bytes
        )
        if overflow <= 0:
            return

        first_stream_name: OutputStreamName = (
            "stderr" if newest_stream_name == "stdout" else "stdout"
        )
        overflow = self._trim_foreground_stream(session, first_stream_name, overflow)
        if overflow > 0:
            self._trim_foreground_stream(session, newest_stream_name, overflow)
        session.truncated = True

    @staticmethod
    def _trim_foreground_stream(
        session: ProcessSession,
        stream_name: OutputStreamName,
        bytes_to_remove: int,
    ) -> int:
        chunks = session.stdout_lines if stream_name == "stdout" else session.stderr_lines
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
                session.foreground_stdout_bytes -= removed
            else:
                session.foreground_stderr_bytes -= removed

        return bytes_to_remove

    def _enforce_buffer_cap(self, session: ProcessSession) -> None:
        overflow = len(session.combined_buffer) - self._buffer_cap_bytes
        if overflow <= 0:
            return

        del session.combined_buffer[:overflow]
        session.buffer_start_offset += overflow
        session.truncated = True
        session.output_chunks = [
            chunk
            for chunk in session.output_chunks
            if chunk.end_offset > session.buffer_start_offset
        ]

    async def _kill_session(self, session: ProcessSession) -> None:
        if session.status != "running":
            return

        self._kill_session_now(session)
        if session.wait_task is not None:
            await asyncio.gather(session.wait_task, return_exceptions=True)

    def _kill_session_now(self, session: ProcessSession) -> None:
        if session.status != "running":
            return

        session.status = "killed"
        self._close_stdin(session)
        try:
            self._kill_process_tree(session.proc)
        except ProcessLookupError:
            session.finished_at = _utc_now()
            session.stdin_open = False
        session.output_event.set()

    @staticmethod
    def _kill_process_tree(proc: Process) -> None:
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if completed.returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass

            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return

        try:
            kill_process_group = cast(Any, os).__dict__["killpg"]
            kill_process_group(proc.pid, HARD_KILL_SIGNAL)
        except ProcessLookupError:
            raise
        except OSError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    @staticmethod
    def _close_stdin(session: ProcessSession) -> None:
        stdin = session.proc.stdin
        if stdin is None or not session.stdin_open:
            return

        try:
            if stdin.can_write_eof():
                stdin.write_eof()
            else:
                stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            stdin.close()
        session.stdin_open = False

    def _session_for_agent(self, session_id: str, agent_id: str) -> ProcessSession:
        session = self._sessions.get(session_id)
        if session is None or session.agent_id != agent_id:
            raise SessionNotFoundError(f"Process session not found: {session_id}")
        return session

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sweep_interval_seconds)
                await self.sweep_finished()
        except asyncio.CancelledError:
            return


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
    return data.decode("utf-8", errors="replace")


def _is_waiting_for_input(session: ProcessSession) -> bool:
    if not session.stdin_open:
        return False

    last_activity_at = session.last_output_at or session.started_at
    return (_utc_now() - last_activity_at).total_seconds() >= INPUT_IDLE_SECONDS


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "FINISHED_SESSION_TTL",
    "INPUT_IDLE_SECONDS",
    "PROCESS_BUFFER_CAP_BYTES",
    "ProcessManager",
    "ProcessManagerError",
    "ProcessSession",
    "ProcessStatus",
    "SessionInputClosedError",
    "SessionNotFoundError",
    "SessionStillRunningError",
]
