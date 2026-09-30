"""Live, non-canonical activity transcripts for Sub-Agent Runs.

The Event Loop only builds each file's Markdown text. One process-wide ordered
writer thread creates, appends to and completes every activity file, so a slow
or stalled disk never blocks the loop and each file receives its text in the
order it was produced. Buffered text is handed to the writer at most every
``_FLUSH_INTERVAL_SECONDS`` and at once when the file ends; the writer appends
each chunk with its own open, write and close, so the file stays readable while
the Run works.

When too many chunks wait for the writer, new Assistant output text is dropped
and a note marks the gap; headings, Tool lines and status lines are always
written. A watcher that falls behind its Run's event stream still writes the
Run's outcome once the Run has ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_INTERRUPTED_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    Run,
    RunEvent,
    RunStatus,
)
from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.utils.logging import get_logger
from core.utils.workers import OrderedWorker

_LOGGER = get_logger("subagents.activity")

_WRITER = OrderedWorker(name="subagent-activity")
# Handed-off chunks (of every activity file) beyond which a chunk holding only
# Assistant output text is dropped. Chunks with headings, Tool lines or status
# lines are always handed off.
_PENDING_WRITE_LIMIT = 256
_NO_LIMIT = sys.maxsize
# Readers poll the file for progress, not tokens; batching bounds hand-offs.
_FLUSH_INTERVAL_SECONDS = 0.25
_SECTION_END = "\n\n"
# Read by the Parent Agent in place of text that never reached the file.
_MISSING_ACTIVITY_NOTE = (
    "_[Part of the activity is missing here; the Sub-Agent's result is not affected.]_\n\n"
)
_DROPPED_TEXT_NOTE = _SECTION_END + _MISSING_ACTIVITY_NOTE


class SubAgentActivity:
    """Own one Sub-Agent Run's temporary Markdown activity file.

    Only the writer thread touches the file and its lease; every other method
    runs on the Event Loop and never waits for the disk.
    """

    def __init__(self, lease: TemporaryFileLease, path: Path) -> None:
        self._lease = lease
        self._path = path
        self._run: Run | None = None
        # Strong reference: the event loop keeps only weak references to tasks,
        # so an unsaved watch task may be garbage-collected mid-execution.
        self._watch_task: asyncio.Task[None] | None = None
        # Set once the final text and the lease completion are handed off.
        self._closed = False
        self._buffer: list[str] = []
        # Whether the buffer holds text that must never be dropped.
        self._buffer_kept = False
        self._flush_timer: asyncio.TimerHandle | None = None
        # Assistant text was dropped; the next handed-off chunk marks the gap.
        self._text_dropped = False
        self._drop_logged = False
        # Writer thread only: a failed append lost text the next one marks.
        self._write_failed = False
        self._write_failure_logged = False

    @property
    def path(self) -> Path:
        """Return the activity file's resolved path."""
        return self._path

    @classmethod
    async def create(
        cls,
        temporary_files: TemporaryFileManager,
        *,
        agent_id: str,
        session_id: str,
    ) -> SubAgentActivity | None:
        """Allocate the file and write non-sensitive identity metadata.

        The file is created on the writer thread; ``None`` means it is
        unavailable, which affects observability only.
        """
        header = (
            "# Sub-Agent activity\n\n"
            f"- Agent: `{agent_id}`\n"
            f"- Session: `{session_id}`\n"
            f"- Created: {_utc_timestamp()}\n\n"
        )
        created: list[SubAgentActivity] = []

        def allocate() -> None:
            activity = cls._allocate(temporary_files, header, agent_id, session_id)
            if activity is not None:
                created.append(activity)

        try:
            await _WRITER.call_async(allocate)
        except asyncio.CancelledError:
            # The file exists but nobody will follow it: start its retention.
            for activity in created:
                activity._close()
            raise
        return created[0] if created else None

    @classmethod
    def _allocate(
        cls,
        temporary_files: TemporaryFileManager,
        header: str,
        agent_id: str,
        session_id: str,
    ) -> SubAgentActivity | None:
        lease: TemporaryFileLease | None = None
        try:
            lease = temporary_files.create("subagents", ".md")
            path = lease.path.resolve()
            path.write_text(header, encoding="utf-8", newline="")
        except OSError as error:
            if lease is not None:
                lease.finish()
            _LOGGER.warning(
                "Sub-agent activity file unavailable agent=%s session=%s: %s",
                agent_id,
                session_id,
                error,
            )
            return None
        return cls(lease, path)

    def mark_queued(self) -> None:
        """Record that the admitted Run is waiting for its Session turn."""
        self._write(_status_text(_utc_timestamp(), "queued"), kept=True)

    def attach(self, run: Run) -> None:
        """Start replaying and following one Run's visible activity events."""
        if self._run is not None or self._closed:
            return
        self._run = run
        self._watch_task = asyncio.create_task(
            self._watch(run),
            name=f"subagent-activity:{run.id}",
        )

    def finish_unstarted(self, status: str = "cancelled before start") -> None:
        """Finalize a queued activity file whose Run will never start."""
        if self._run is not None or self._closed:
            return
        self._write(_status_text(_utc_timestamp(), status), kept=True)
        self._close()

    async def drain(self) -> None:
        """Wait until the text produced so far is on disk.

        Once the followed Run has ended this includes its outcome: the watcher
        finishes first. A Run still working is not waited for.
        """
        run, task = self._run, self._watch_task
        if run is not None and task is not None and run.status is not RunStatus.RUNNING:
            await asyncio.wait({task})
        if not self._closed:
            self._flush()
        await _WRITER.drain()

    async def _watch(self, run: Run) -> None:
        assistant_open = False
        assistant_streamed = False
        outcome: tuple[str, str] | None = None
        try:
            self._write(_status_text(_utc_timestamp(), "running", run_id=run.id), kept=True)
            async for event in run.subscribe():
                if event.type == ASSISTANT_OUTPUT_DELTA_EVENT:
                    delta = event.payload.get("content_delta")
                    if not isinstance(delta, str) or not delta:
                        continue
                    if not assistant_open:
                        self._write(_heading(event.timestamp, "Assistant"), kept=True)
                        assistant_open = True
                    self._write(delta, kept=False)
                    assistant_streamed = True
                    continue

                if event.type == ASSISTANT_OUTPUT_EVENT:
                    message = event.payload.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if assistant_open and assistant_streamed:
                        self._write(_SECTION_END, kept=True)
                    elif isinstance(content, str) and content:
                        self._write(
                            _heading(event.timestamp, "Assistant") + content + _SECTION_END,
                            kept=True,
                        )
                    assistant_open = False
                    assistant_streamed = False
                    continue

                if event.type in (TOOL_CALL_STARTED_EVENT, TOOL_CALL_RESULT_EVENT):
                    if assistant_open:
                        self._write(_SECTION_END, kept=True)
                        assistant_open = False
                        assistant_streamed = False
                    self._write(_tool_text(event), kept=True)
                    continue

                terminal_status = _terminal_status(event)
                if terminal_status is not None:
                    if assistant_open:
                        self._write(_SECTION_END, kept=True)
                        assistant_open = False
                    outcome = (event.timestamp, terminal_status)

            if outcome is None:
                # The stream ended before the Run did: the Run evicted this
                # watcher for falling behind. Mark the gap and take the outcome
                # from the Run once it has ended.
                _LOGGER.warning(
                    "Sub-agent activity missed Run events (run=%s path=%s)", run.id, self.path
                )
                if assistant_open:
                    self._write(_SECTION_END, kept=True)
                self._write(_MISSING_ACTIVITY_NOTE, kept=True)
                await _run_ended(run)
                outcome = (run.updated_at, run.status.value)
            self._write(_status_text(*outcome, run_id=run.id), kept=True)
        except Exception as error:
            _LOGGER.warning(
                "Sub-agent activity watcher stopped path=%s run=%s: %s",
                self.path,
                run.id,
                error,
            )
        finally:
            self._close()

    def _write(self, text: str, *, kept: bool) -> None:
        """Buffer text for the writer; it is handed off within the flush interval."""
        if self._closed:
            return
        self._buffer.append(text)
        self._buffer_kept = self._buffer_kept or kept
        if self._flush_timer is None:
            self._flush_timer = asyncio.get_running_loop().call_later(
                _FLUSH_INTERVAL_SECONDS, self._flush
            )

    def _flush(self) -> None:
        """Hand the buffered text to the writer without waiting for it."""
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None
        if not self._buffer:
            return
        text = "".join(self._buffer)
        kept = self._buffer_kept
        self._buffer.clear()
        self._buffer_kept = False
        if self._text_dropped:
            text = _DROPPED_TEXT_NOTE + text
        limit = _NO_LIMIT if kept else _PENDING_WRITE_LIMIT
        if _WRITER.hand_off(partial(self._append, text), limit=limit):
            self._text_dropped = False
            return
        self._text_dropped = True
        if not self._drop_logged:
            self._drop_logged = True
            _LOGGER.warning(
                "Sub-agent activity output dropped: its writer is behind (path=%s)", self.path
            )

    def _close(self) -> None:
        """Hand off the remaining text and the lease completion; ignore later text."""
        if self._closed:
            return
        self._flush()
        self._closed = True
        _WRITER.hand_off(self._lease.finish, limit=_NO_LIMIT)

    def _append(self, text: str) -> None:
        """Append one chunk on the writer thread; the close flushes it."""
        try:
            with self._path.open("a", encoding="utf-8", errors="replace", newline="") as handle:
                if self._write_failed:
                    handle.write(_DROPPED_TEXT_NOTE)
                handle.write(text)
        except OSError as error:
            self._write_failed = True
            if not self._write_failure_logged:
                self._write_failure_logged = True
                _LOGGER.warning("Sub-agent activity write failed (path=%s): %s", self._path, error)
            return
        self._write_failed = False


async def _run_ended(run: Run) -> None:
    """Wait until *run* has ended, whatever its outcome."""
    # ``Run.wait`` reports a failed, cancelled or interrupted outcome by raising
    # it; the caller reads the outcome from ``run.status`` instead.
    with contextlib.suppress(Exception):
        await run.wait()


def _heading(timestamp: str, label: str) -> str:
    return f"## {timestamp} — {label}\n\n"


def _tool_text(event: RunEvent) -> str:
    tool_call = event.payload.get("tool_call")
    name = tool_call.get("name") if isinstance(tool_call, dict) else None
    if not isinstance(name, str) or not name:
        name = "unknown"
    if event.type == TOOL_CALL_STARTED_EVENT:
        display = event.payload.get("display")
        summary = display.get("summary") if isinstance(display, dict) else None
        detail = f" — {summary}" if isinstance(summary, str) and summary.strip() else ""
        state = f"started{detail}"
    else:
        result = event.payload.get("result")
        completed = isinstance(result, dict) and result.get("ok") is True
        state = "completed" if completed else "failed"
    return f"{_heading(event.timestamp, 'Tool')}`{name}` {state}\n\n"


def _status_text(timestamp: str, status: str, *, run_id: str | None = None) -> str:
    run_detail = f" (`{run_id}`)" if run_id else ""
    return f"{_heading(timestamp, 'Run status')}{status}{run_detail}\n\n"


def _terminal_status(event: RunEvent) -> str | None:
    return {
        RUN_COMPLETED_EVENT: "completed",
        RUN_FAILED_EVENT: "failed",
        RUN_CANCELLED_EVENT: "cancelled",
        RUN_INTERRUPTED_EVENT: "interrupted",
    }.get(event.type)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["SubAgentActivity"]
