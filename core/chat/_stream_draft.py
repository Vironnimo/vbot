"""Crash-safe copy of the Model output a Run streams before it persists."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from core.utils.logging import get_logger
from core.utils.workers import cancel_and_wait

if TYPE_CHECKING:
    from core.chat.messages import ToolCall
    from core.sessions import ChatSession

STREAM_DRAFT_FLUSH_INTERVAL_SECONDS = 2.0

# Failed writes in a row after which a stream attempt stops storing its draft.
_MAX_FAILED_WRITES = 3

_LOGGER = get_logger("chat")


class StreamDraft:
    """Store a Run's streamed output that no Assistant entry holds yet, every few seconds.

    Restart recovery turns the stored draft into the Run's interrupted
    Assistant entry. Persisting the Model step's Assistant entry deletes the
    draft in the same transaction, so :meth:`settle` runs first: no draft write
    commits after that entry. A stream attempt that ends without an Assistant
    entry is :meth:`discard`-ed; its output never becomes history.

    Tool Calls that start during the stream are written at once, not after the
    flush interval: they may have effects that history must show.

    A failed write keeps its output, in order, for the next write, which waits
    out the flush interval even for started Tool Calls. After
    ``_MAX_FAILED_WRITES`` failures in a row the stream attempt stops storing:
    the stored chunks stay a prefix of its output.
    """

    def __init__(
        self,
        session: ChatSession,
        *,
        flush_interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = session
        self._flush_interval = (
            STREAM_DRAFT_FLUSH_INTERVAL_SECONDS if flush_interval is None else flush_interval
        )
        self._clock = clock
        self._sleep = sleep
        self._model = ""
        self._reasoning: list[str] = []
        self._content: list[str] = []
        self._tool_calls: list[dict[str, Any]] = []
        self._timer: asyncio.Task[None] | None = None
        self._write: asyncio.Task[None] | None = None
        self._last_write = clock()
        # Whether stored chunks may exist that no Assistant entry deleted yet.
        self._stored = False
        self._draining = False
        self._failed_writes = 0
        # Whether this stream attempt stopped storing after failed writes.
        self._stopped = False

    def record(self, *, model: str, reasoning: str, content: str) -> None:
        """Add streamed deltas; a write follows within the flush interval."""
        if self._stopped or (not reasoning and not content):
            return
        self._model = model
        if reasoning:
            self._reasoning.append(reasoning)
        if content:
            self._content.append(content)
        self._schedule()

    def record_tool_calls(self, *, model: str, tool_calls: Sequence[ToolCall]) -> None:
        """Add Tool Calls that just started; their write follows at once."""
        if self._stopped or not tool_calls:
            return
        self._model = model
        self._tool_calls.extend(call.to_dict() for call in tool_calls)
        timer, self._timer = self._timer, None
        if timer is not None:
            # A write waiting for the flush interval goes now instead.
            timer.cancel()
        self._schedule()

    async def settle(self) -> None:
        """Wait for a running write and drop unwritten deltas.

        The caller's next write is the Assistant entry that holds this output
        and deletes the stored draft, or the Run's completion, which does too.
        """
        await self._drain()
        self._end_attempt()
        self._stored = False

    async def discard(self) -> None:
        """Delete the draft of a stream attempt whose output will not be persisted."""
        await self._drain()
        self._end_attempt()
        if not self._stored:
            return
        try:
            await self._session.discard_stream_draft_async()
            self._stored = False
        except Exception:
            _LOGGER.warning("Failed to discard the stream draft", exc_info=True)

    def _schedule(self) -> None:
        if self._timer is None and self._write is None and not self._draining:
            self._timer = asyncio.create_task(self._write_later(), name="run-stream-draft")

    async def _write_later(self) -> None:
        # Started Tool Calls go at once, but not right after a failed write:
        # retrying at once would hammer a store that just failed.
        if not self._tool_calls or self._failed_writes:
            await self._sleep(max(0.0, self._flush_interval - (self._clock() - self._last_write)))
        self._timer = None
        if not self._has_pending():
            return
        chunk = (
            self._model,
            "".join(self._reasoning),
            "".join(self._content),
            list(self._tool_calls),
        )
        self._clear_pending()
        # A running write is never cancelled: settle() waits for it instead.
        self._write = asyncio.create_task(self._store(*chunk), name="run-stream-draft-write")

    async def _store(
        self, model: str, reasoning: str, content: str, tool_calls: list[dict[str, Any]]
    ) -> None:
        try:
            await self._session.append_stream_draft_async(
                model=model,
                reasoning_delta=reasoning,
                content_delta=content,
                tool_calls=tool_calls,
            )
            self._stored = True
            self._failed_writes = 0
        except Exception:
            self._failed_writes += 1
            if self._failed_writes < _MAX_FAILED_WRITES:
                _LOGGER.debug(
                    "Failed to store the stream draft (run=%s failures=%d)",
                    self._session.run_id,
                    self._failed_writes,
                    exc_info=True,
                )
                # Keep the output in order for the next write.
                self._reasoning.insert(0, reasoning)
                self._content.insert(0, content)
                self._tool_calls[:0] = tool_calls
            else:
                # Output that never stores would hold back all later output;
                # skipping it would leave a gap in what restart recovery keeps.
                self._stopped = True
                self._clear_pending()
                _LOGGER.warning(
                    "Stopped storing the stream draft after failed writes (run=%s failures=%d)",
                    self._session.run_id,
                    self._failed_writes,
                    exc_info=True,
                )
        finally:
            self._last_write = self._clock()
            self._write = None
            if self._has_pending():
                self._schedule()

    async def _drain(self) -> None:
        # The caller drops or deletes what is still pending, so nothing new is scheduled.
        self._draining = True
        try:
            timer, self._timer = self._timer, None
            if timer is not None:
                await cancel_and_wait(timer)
            write = self._write
            if write is not None:
                await asyncio.shield(write)
        finally:
            self._draining = False

    def _end_attempt(self) -> None:
        """Drop unwritten output; the next stream attempt stores its draft afresh."""
        self._clear_pending()
        self._failed_writes = 0
        self._stopped = False

    def _has_pending(self) -> bool:
        return bool(self._reasoning or self._content or self._tool_calls)

    def _clear_pending(self) -> None:
        self._reasoning.clear()
        self._content.clear()
        self._tool_calls.clear()
