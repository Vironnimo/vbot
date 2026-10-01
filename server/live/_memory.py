"""What the Live operator remembers from one call to the next.

A later call keeps the refs earlier calls named (``s2`` still names the same
Session) and knows the operator's latest assignments, so the user can pick up
where they left off ("what did Coder say to the task from earlier?"). The
memory lives in this server process only: it is forgotten once no call ran and
no Live Tool was used for ``idle_seconds`` (8 hours), and at a restart.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.model_tasks.live import live_result_text
from server.live._brief import LIVE_READ_ONLY_TOOLS, TOOL_END_CALL, TOOL_OPEN, TOOL_TERMINAL
from server.live._targets import LiveRefs

JsonObject = dict[str, Any]

_IDLE_SECONDS = 8 * 3600.0
_MAX_NOTES = 8
_NOTE_VALUE_CHARS = 120
_NOTE_RESULT_CHARS = 240
# Tool calls that only look or arrange the app are not assignments.
_NOT_NOTED = LIVE_READ_ONLY_TOOLS | {TOOL_OPEN, TOOL_END_CALL}
_NOTED_TERMINAL_ACTIONS = frozenset(
    {"key", "close", "create_group", "rename_group", "delete_group"}
)


@dataclass(frozen=True)
class _Note:
    at: float
    line: str


class LiveMemory:
    """The refs and latest assignments of the Live calls of the last hours.

    :meth:`begin_call` opens a call: it forgets an idle memory and returns the
    recap of earlier calls for the Models' instructions. Executions report
    every Tool call they ran through :meth:`note`.
    """

    def __init__(
        self, *, idle_seconds: float = _IDLE_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._forget()

    def _forget(self) -> None:
        self.refs = LiveRefs()
        self._notes: deque[_Note] = deque(maxlen=_MAX_NOTES)
        self._used_at: float | None = None

    def begin_call(self) -> str:
        """Open a call; returns what earlier calls did, ``""`` when nothing is known."""
        now = self._clock()
        if self._used_at is not None and now - self._used_at > self._idle_seconds:
            self._forget()
        self._used_at = now
        lines = [f"- {_age(now - note.at)}: {note.line}" for note in self._notes]
        legend = self.refs.legend()
        parts = []
        if lines:
            parts.append("What you did, oldest first:\n" + "\n".join(lines))
        if legend:
            parts.append("Refs:\n" + legend)
        return "\n".join(parts)

    def touch(self) -> None:
        """Mark the memory as in use, so it is kept for another idle period."""
        self._used_at = self._clock()

    def note(self, tool: str, arguments: JsonObject, result: JsonObject) -> None:
        """Remember one executed Tool call when it was an assignment."""
        self.touch()
        if tool in _NOT_NOTED:
            return
        if tool == TOOL_TERMINAL and arguments.get("action") not in _NOTED_TERMINAL_ACTIONS:
            return
        shown = json.dumps(
            {key: _short(value, _NOTE_VALUE_CHARS) for key, value in arguments.items()},
            ensure_ascii=False,
        )
        outcome = _short(live_result_text(result), _NOTE_RESULT_CHARS)
        self._notes.append(_Note(at=self._clock(), line=f"{tool} {shown} -> {outcome}"))


def _short(value: Any, limit: int) -> Any:
    if not isinstance(value, str):
        return value
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute ago"
    if minutes < 60:
        return f"{minutes} min ago"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min ago" if minutes else f"{hours} h ago"
