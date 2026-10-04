"""Crash-safe copy of the Model output a Run streams before it persists."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING

from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.sessions import ChatSession

STREAM_DRAFT_FLUSH_INTERVAL_SECONDS = 2.0

_LOGGER = get_logger("chat")


class StreamDraft:
    """Store a Run's streamed output that no Assistant entry holds yet, every few seconds.

    Restart recovery turns the stored draft into the Run's interrupted
    Assistant entry. Persisting the Model step's Assistant entry deletes the
    draft in the same transaction, so :meth:`settle` runs first: no draft write
    commits after that entry. A stream attempt that ends without an Assistant
    entry is :meth:`discard`-ed; its output never becomes history.
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
        self._timer: asyncio.Task[None] | None = None
        self._write: asyncio.Task[None] | None = None
        self._last_write = clock()
        # Whether stored chunks may exist that no Assistant entry deleted yet.
        self._stored = False
        self._draining = False

    def record(self, *, model: str, reasoning: str, content: str) -> None:
        """Add streamed deltas; a write follows within the flush interval."""
        if not reasoning and not content:
            return
        self._model = model
        if reasoning:
            self._reasoning.append(reasoning)
        if content:
            self._content.append(content)
        self._schedule()

    async def settle(self) -> None:
        """Wait for a running write and drop unwritten deltas.

        The caller's next write is the Assistant entry that holds this output
        and deletes the stored draft, or the Run's completion, which does too.
        """
        await self._drain()
        self._clear_pending()
        self._stored = False

    async def discard(self) -> None:
        """Delete the draft of a stream attempt whose output will not be persisted."""
        await self._drain()
        self._clear_pending()
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
        await self._sleep(max(0.0, self._flush_interval - (self._clock() - self._last_write)))
        self._timer = None
        if not self._reasoning and not self._content:
            return
        chunk = (self._model, "".join(self._reasoning), "".join(self._content))
        self._clear_pending()
        # A running write is never cancelled: settle() waits for it instead.
        self._write = asyncio.create_task(self._store(*chunk), name="run-stream-draft-write")

    async def _store(self, model: str, reasoning: str, content: str) -> None:
        try:
            await self._session.append_stream_draft_async(
                model=model, reasoning_delta=reasoning, content_delta=content
            )
            self._stored = True
        except Exception:
            _LOGGER.warning("Failed to store the stream draft", exc_info=True)
            # Keep the text in order for the next write.
            self._reasoning.insert(0, reasoning)
            self._content.insert(0, content)
        finally:
            self._last_write = self._clock()
            self._write = None
            if self._reasoning or self._content:
                self._schedule()

    async def _drain(self) -> None:
        # The caller drops or deletes what is still pending, so nothing new is scheduled.
        self._draining = True
        try:
            timer, self._timer = self._timer, None
            if timer is not None:
                timer.cancel()
                with suppress(asyncio.CancelledError):
                    await timer
            write = self._write
            if write is not None:
                await asyncio.shield(write)
        finally:
            self._draining = False

    def _clear_pending(self) -> None:
        self._reasoning.clear()
        self._content.clear()
