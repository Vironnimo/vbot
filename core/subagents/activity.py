"""Live, non-canonical activity transcripts for Sub-Agent Runs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    Run,
    RunEvent,
)
from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.utils.logging import get_logger

_LOGGER = get_logger("subagents.activity")


class SubAgentActivity:
    """Own one Sub-Agent Run's temporary Markdown activity file."""

    def __init__(self, lease: TemporaryFileLease) -> None:
        self._lease = lease
        self._finished = False
        self._attached = False

    @property
    def path(self) -> Path:
        return self._lease.path

    @classmethod
    def create(
        cls,
        temporary_files: TemporaryFileManager,
        *,
        agent_id: str,
        session_id: str,
    ) -> SubAgentActivity | None:
        """Allocate the file and write non-sensitive identity metadata."""
        lease: TemporaryFileLease | None = None
        try:
            lease = temporary_files.create("subagents", ".md")
            activity = cls(lease)
            activity.path.write_text(
                "# Sub-Agent activity\n\n"
                f"- Agent: `{agent_id}`\n"
                f"- Session: `{session_id}`\n"
                f"- Created: {_utc_timestamp()}\n\n",
                encoding="utf-8",
            )
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
        return activity

    def mark_queued(self) -> None:
        """Record that the admitted Run is waiting for its Session turn."""
        self._append_status("queued")

    def attach(self, run: Run) -> None:
        """Start replaying and following one Run's visible activity events."""
        if self._attached or self._finished:
            return
        self._attached = True
        asyncio.create_task(
            self._watch(run),
            name=f"subagent-activity:{run.id}",
        )

    def finish_unstarted(self, status: str = "cancelled before start") -> None:
        """Finalize a queued activity file whose Run will never start."""
        if self._finished:
            return
        self._append_status(status)
        self._finish_lease()

    def _append_status(self, status: str) -> None:
        try:
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                _write_status(handle, _utc_timestamp(), status)
        except OSError as error:
            _LOGGER.warning("Sub-agent activity status write failed path=%s: %s", self.path, error)

    async def _watch(self, run: Run) -> None:
        assistant_open = False
        assistant_streamed = False
        try:
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                _write_status(handle, _utc_timestamp(), "running", run_id=run.id)
                async for event in run.subscribe():
                    if event.type == ASSISTANT_OUTPUT_DELTA_EVENT:
                        delta = event.payload.get("content_delta")
                        if not isinstance(delta, str) or not delta:
                            continue
                        if not assistant_open:
                            _write_heading(handle, event.timestamp, "Assistant")
                            assistant_open = True
                        handle.write(delta)
                        handle.flush()
                        assistant_streamed = True
                        continue

                    if event.type == ASSISTANT_OUTPUT_EVENT:
                        message = event.payload.get("message")
                        content = message.get("content") if isinstance(message, dict) else None
                        if assistant_open and assistant_streamed:
                            _close_section(handle)
                        elif isinstance(content, str) and content:
                            _write_heading(handle, event.timestamp, "Assistant")
                            handle.write(content)
                            _close_section(handle)
                        assistant_open = False
                        assistant_streamed = False
                        continue

                    if event.type == TOOL_CALL_STARTED_EVENT:
                        if assistant_open:
                            _close_section(handle)
                            assistant_open = False
                            assistant_streamed = False
                        _write_tool_started(handle, event)
                        continue

                    if event.type == TOOL_CALL_RESULT_EVENT:
                        if assistant_open:
                            _close_section(handle)
                            assistant_open = False
                            assistant_streamed = False
                        _write_tool_finished(handle, event)
                        continue

                    terminal_status = _terminal_status(event)
                    if terminal_status is not None:
                        if assistant_open:
                            _close_section(handle)
                            assistant_open = False
                        _write_status(handle, event.timestamp, terminal_status, run_id=run.id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(
                "Sub-agent activity watcher stopped path=%s run=%s: %s",
                self.path,
                run.id,
                error,
            )
        finally:
            self._finish_lease()

    def _finish_lease(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._lease.finish()


def _write_heading(handle: TextIO, timestamp: str, label: str) -> None:
    handle.write(f"## {timestamp} — {label}\n\n")
    handle.flush()


def _close_section(handle: TextIO) -> None:
    handle.write("\n\n")
    handle.flush()


def _write_tool_started(handle: TextIO, event: RunEvent) -> None:
    tool_call = event.payload.get("tool_call")
    name = tool_call.get("name") if isinstance(tool_call, dict) else None
    if not isinstance(name, str) or not name:
        name = "unknown"
    display = event.payload.get("display")
    summary = display.get("summary") if isinstance(display, dict) else None
    detail = f" — {summary}" if isinstance(summary, str) and summary.strip() else ""
    _write_heading(handle, event.timestamp, "Tool")
    handle.write(f"`{name}` started{detail}\n\n")
    handle.flush()


def _write_tool_finished(handle: TextIO, event: RunEvent) -> None:
    tool_call = event.payload.get("tool_call")
    name = tool_call.get("name") if isinstance(tool_call, dict) else None
    if not isinstance(name, str) or not name:
        name = "unknown"
    result = event.payload.get("result")
    completed = isinstance(result, dict) and result.get("ok") is True
    status = "completed" if completed else "failed"
    _write_heading(handle, event.timestamp, "Tool")
    handle.write(f"`{name}` {status}\n\n")
    handle.flush()


def _write_status(
    handle: TextIO, timestamp: str, status: str, *, run_id: str | None = None
) -> None:
    _write_heading(handle, timestamp, "Run status")
    run_detail = f" (`{run_id}`)" if run_id else ""
    handle.write(f"{status}{run_detail}\n\n")
    handle.flush()


def _terminal_status(event: RunEvent) -> str | None:
    return {
        RUN_COMPLETED_EVENT: "completed",
        RUN_FAILED_EVENT: "failed",
        RUN_CANCELLED_EVENT: "cancelled",
    }.get(event.type)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["SubAgentActivity"]
