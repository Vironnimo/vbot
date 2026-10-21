"""Session activity as the server reports it, for the status row and the Session picker.

The server owns one activity state per Session: ``working`` (a Run or queued
input), ``waiting`` (no Run, but pending work that will wake the Session again:
Sub-Agents, background commands, results not yet delivered) or ``idle``. The TUI
keeps the Sessions that are not idle from the ``/ws`` hello's
``session_activity`` and each ``session_activity_changed`` event instead of
reconstructing the state from Run events. Against a server that reports no
activity, the status row follows the displayed Run as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from cli.formatting import duration_text
from cli.tui.state import Conversation, JsonObject

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# The picker draws working as a filled dot and waiting as its ring, as the WebUI
# does; a result counts as unread only once its Session is idle.
_MARKS = {"working": "● ", "waiting": "○ "}
_UNREAD_MARK = "• "
_WAITING_NOUNS = (
    ("subagents", "Sub-Agent", "Sub-Agents"),
    ("commands", "command", "commands"),
    ("results", "result", "results"),
)


def _now() -> datetime:
    """The clock elapsed work spans count against; tests replace it."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class Activity:
    """A Session that is not idle, as the server last reported it."""

    state: str
    work_started_at: datetime | None
    waiting_for: str


class SessionActivity:
    """The non-idle Sessions of the connected server, keyed by Agent address and Session."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], Activity] = {}

    def connection_ready(self, hello: JsonObject) -> None:
        """Start over from a hello: a Session it does not list is idle."""
        self._sessions = {}
        rows = hello.get("session_activity")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                self.changed(row)

    def changed(self, payload: JsonObject) -> None:
        """Apply one ``session_activity_changed`` payload or hello row."""
        key = _session_key(payload)
        if key is None:
            return
        state = payload.get("state")
        if state in _MARKS:
            self._sessions[key] = Activity(
                str(state),
                _instant(payload.get("work_started_at")),
                _waiting_text(payload.get("waiting_for")),
            )
        else:
            self._sessions.pop(key, None)

    def of(self, view: Conversation) -> Activity | None:
        """The displayed Session's activity; ``None`` while it is idle or unknown."""
        if not view.session:
            return None
        return self._sessions.get((view.agent, view.session))

    def status(self, view: Conversation) -> str:
        """The status row's work line for ``view``; empty while nothing works for it.

        A working Session counts its whole work span, across Runs and waiting; a
        waiting one names what it waits for and the key that ends that work.
        """
        activity = self.of(view)
        active = view.facts.get("active_run") or {}
        if activity is not None and activity.state == "waiting":
            reasons = f" for {activity.waiting_for}" if activity.waiting_for else ""
            return f"○ Waiting{reasons}{_elapsed(activity.work_started_at)} · F10 stops all"
        if activity is None and not active:
            return ""
        spinner = _SPINNER[int(monotonic() * 10) % 10]
        status = str(active.get("status") or "working")
        label = "Working" if status in {"running", "working"} else status.capitalize()
        started = activity.work_started_at if activity is not None else None
        # A server without activity reports, or a Run whose working state has not
        # arrived yet, counts from the displayed Run's start.
        return f"{spinner} {label}{_elapsed(started or _run_started(view))} · Ctrl+X stops"


def session_mark(row: JsonObject) -> str:
    """The Session picker's mark for a ``session.list`` row."""
    state = row.get("activity_state")
    if state is None and row.get("has_active_run"):
        # A server that does not report activity.
        state = "working"
    if isinstance(state, str) and state in _MARKS:
        return _MARKS[state]
    return _UNREAD_MARK if row.get("has_unread_completion") is True else ""


def _session_key(payload: JsonObject) -> tuple[str, str] | None:
    agent, session = payload.get("agent_id"), payload.get("session_id")
    if not isinstance(agent, str) or not agent or not isinstance(session, str) or not session:
        return None
    project = payload.get("project_id")
    return (f"{agent}@{project}" if isinstance(project, str) and project else agent), session


def _waiting_text(value: object) -> str:
    """``2 Sub-Agents, 1 command`` from the server's ``waiting_for`` counts."""
    counts = value if isinstance(value, dict) else {}
    parts: list[str] = []
    for field, one, many in _WAITING_NOUNS:
        count = counts.get(field)
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            parts.append(f"{count} {one if count == 1 else many}")
    return ", ".join(parts)


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return None
    return instant if instant.tzinfo else instant.replace(tzinfo=UTC)


def _run_started(view: Conversation) -> datetime | None:
    """When the displayed running Run started, from its timeline row."""
    for item in reversed(view.items):
        if item.get("type") == "assistant_run" and item.get("status") == "running":
            return _instant(item.get("startTimestamp") or item.get("timestamp"))
    return None


def _elapsed(started: datetime | None) -> str:
    if started is None:
        return ""
    return f" · {duration_text((_now() - started).total_seconds() * 1000)}"
