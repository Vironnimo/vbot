"""Terminal observation waits on the Session clock."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from ._terminal_activity import (
    TerminalActivity,
)
from ._terminal_attention import TerminalNotices
from ._terminal_output import TerminalOutput
from ._terminal_state import (
    TerminalSessionServices,
    WaitEnded,
)


class TerminalWaits:
    """Observe screen/activity changes without consuming the Agent's durable view."""

    def __init__(
        self,
        output: TerminalOutput,
        notices: TerminalNotices,
        activity: TerminalActivity,
        services: TerminalSessionServices,
        lock: asyncio.Lock,
    ) -> None:
        self._lock = lock
        self._output = output
        self._notices = notices
        self._activity = activity
        self._services = services
        self.change = asyncio.Event()
        self.quiet_boundaries = 0
        self.screen_shown = False

    async def wait_or_sleep(self, event: asyncio.Event, seconds: float) -> None:
        """Wait until *event* is set or *seconds* pass on the session clock."""
        if event.is_set():
            return
        happened = asyncio.ensure_future(event.wait())
        pause = asyncio.ensure_future(self._services.sleep(seconds))
        try:
            await asyncio.wait({happened, pause}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (happened, pause):
                waiter.cancel()
            await asyncio.gather(happened, pause, return_exceptions=True)

    async def wait_for_program(
        self,
        *,
        deadline: float,
        pattern: re.Pattern[str] | None,
        after_revision: int,
        on_match: Callable[[str], None] | None = None,
    ) -> WaitEnded:
        """Wait until the program exits, its output matches *pattern*, it goes idle
        showing a new screen, or *deadline* (a ``monotonic`` time) passes.

        Output printed before the call counts for *pattern*, except output
        before the Agent's last input and that input's echo. The program went
        idle when an ``idle`` attention above *after_revision* exists; with
        *pattern* that does not end the wait. *on_match* receives the line
        where the match starts.
        """
        services = self._services
        match_from: int | None = None
        while True:
            change = self.change
            if self._activity.finished:
                return "exited"
            if pattern is not None:
                matched, match_from = await self._output_matches(pattern, match_from)
                if matched is not None:
                    if on_match is not None:
                        on_match(matched)
                    return "matched"
            attention = self._notices.current
            if (
                pattern is None
                and attention is not None
                and attention.kind == "idle"
                and attention.revision > after_revision
            ):
                return "idle"
            now = services.monotonic()
            if now >= deadline:
                return "timeout"
            await self.wait_or_sleep(change, deadline - now)

    async def wait_for_reply(self, *, deadline: float, after_quiet: int) -> WaitEnded:
        """Wait until the output pauses after input, the program exits, or *deadline* passes.

        *after_quiet* is the ``quiet_boundaries`` count the input reported:
        the output paused once a quiet boundary follows it.
        """
        while True:
            change = self.change
            if self._activity.finished:
                return "exited"
            if self.quiet_boundaries > after_quiet:
                return "quiet"
            now = self._services.monotonic()
            if now >= deadline:
                return "timeout"
            await self.wait_or_sleep(change, deadline - now)

    async def wait_started(self, *, deadline: float) -> None:
        """Wait until an Agent start shows its first screen, at the latest *deadline*.

        The first screen shows once the program printed something visible
        and its output then paused for the quiet period (the start-up's quiet
        boundary, where its idle boundary is decided too), or once the
        program ended. A terminal's own set-up sequences draw nothing, so
        they show no first screen.
        """
        services = self._services
        while not self._activity.finished and not self.screen_shown:
            change = self.change
            now = services.monotonic()
            if now >= deadline:
                return
            await self.wait_or_sleep(change, deadline - now)

    async def _output_matches(
        self, pattern: re.Pattern[str], from_line: int | None
    ) -> tuple[str | None, int]:
        """The line where a match in the output from *from_line* on starts, or None;
        also the line to check from next.

        Without *from_line*, output printed before the wait counts, except
        output before the Agent's last input and its echo. Lines above the
        screen no longer change, so a later check starts at the logical line
        just above the screen (a match can span two lines).
        """
        async with self._lock:
            found = await self._output.screen.pattern_text(start_line=from_line)
        text = found["text"]
        match = pattern.search(text)
        if match is None:
            return None, int(found["next_line"])
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.start())
        return text[start : len(text) if end < 0 else end], int(found["next_line"])

    def signal_change(self) -> None:
        """Wake every waiter: output, input, attention or the program's end changed."""
        self.change.set()
        self.change = asyncio.Event()
