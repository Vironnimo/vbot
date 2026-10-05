"""The tray's activity history: what happened to the server and the application.

The tray host is the only writer: the host lock admits one tray per
installation, and a restarting tray releases it before its successor writes.
Entries persist in a bounded JSON Lines journal in the installation's logs, so
the history survives the tray's restart into a newly activated version. Each
entry carries its finished English text, so a tray of another version shows
entries it does not know. The journal is disposable history: an unreadable
line or file only shortens it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cli.application.monitor import COOPERATIVE_CLOSE_CODES, MonitorStatus
from core.utils.timestamps import format_canonical_timestamp, parse_timestamp

_LOGGER = logging.getLogger("vbot.application.activity")

JOURNAL_NAME = "tray-activity.jsonl"
#: Entries a trimmed journal keeps; it is trimmed once it holds twice as many.
KEEP_ENTRIES = 200
#: Only the end of an oversized journal is read.
_READ_LIMIT_BYTES = 512 * 1024
_LEVELS = frozenset({"info", "warning", "error"})
#: A startup longer than this was not observed from its beginning.
_MAX_STARTUP = timedelta(minutes=30)

_STOP_TEXTS = {
    "cli": "Server stopped from the command line",
    "tray_stop": "Server stopped from the tray",
    "tray_quit": "Server stopped because vBot was quit from the tray",
    "tray_restart": "Server restarting from the tray",
    "update": "Server stopped for an update",
    "scheduled_restart": "Server restarting on its own request",
}
_RESTART_INITIATORS = frozenset({"tray_restart", "scheduled_restart"})


@dataclass(frozen=True)
class ActivityEntry:
    """One recorded event: when (canonical UTC), which kind, what, how severe.

    ``kind`` names the event (``server_started``, ``update_finished``, ...);
    readers show ``text`` and need not know the kind. ``ref`` optionally
    identifies what the entry reports, such as an update operation or one server
    process, so it is recorded only once.
    """

    at: str
    kind: str
    text: str
    level: str = "info"
    ref: str = ""

    def line(self) -> str:
        """Render the entry for the status window in local time."""

        return f"{local_time(self.at, seconds=True)}  {self.text}"


class ActivityJournal:
    """The bounded, persisted list of activity entries; safe to use from any thread."""

    def __init__(self, path: Path, *, now: Callable[[], datetime] | None = None) -> None:
        self._path = path
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._entries, self._lines = _load(path)
        self._revision = 0
        self._rendered: tuple[tuple[int, object], tuple[str, ...]] | None = None

    def lines(self) -> tuple[str, ...]:
        """The entries rendered for display, oldest first.

        Rendered again only when an entry was added or the local date changed,
        so the tray's once-per-second state poll stays cheap.
        """

        key = (self._revision, datetime.now().astimezone().date())
        rendered = self._rendered
        if rendered is not None and rendered[0] == key:
            return rendered[1]
        lines = tuple(entry.line() for entry in self.entries())
        self._rendered = (key, lines)
        return lines

    def entries(self) -> tuple[ActivityEntry, ...]:
        """All kept entries, oldest first."""

        with self._lock:
            return tuple(sorted(self._entries, key=lambda entry: entry.at))

    def has(self, ref: str) -> bool:
        with self._lock:
            return any(entry.ref == ref for entry in self._entries)

    def record(
        self, kind: str, text: str, *, level: str = "info", ref: str = "", at: str = ""
    ) -> None:
        """Add one entry, at ``at`` or now; an entry whose ``ref`` exists is skipped."""

        entry = ActivityEntry(
            at or format_canonical_timestamp(self._now()),
            kind,
            text,
            level if level in _LEVELS else "info",
            ref,
        )
        with self._lock:
            if ref and any(item.ref == ref for item in self._entries):
                return
            self._entries.append(entry)
            self._revision += 1
            self._persist(entry)

    def _persist(self, entry: ActivityEntry) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._lines + 1 >= 2 * KEEP_ENTRIES:
                del self._entries[:-KEEP_ENTRIES]
                temporary = self._path.with_name(f"{self._path.name}.tmp")
                temporary.write_text(
                    "".join(_serialize(item) for item in self._entries), encoding="utf-8"
                )
                os.replace(temporary, self._path)
                self._lines = len(self._entries)
            else:
                with self._path.open("a", encoding="utf-8") as journal:
                    journal.write(_serialize(entry))
                self._lines += 1
        except OSError as error:
            # The history is a convenience; the tray keeps it in memory meanwhile.
            del self._entries[:-KEEP_ENTRIES]
            _LOGGER.warning("Could not write the tray activity journal: %s", error)


class ActivityRecorder:
    """Turn server observations and tray actions into activity entries.

    Monitor observations arrive on the monitor thread, tray actions on the tray's
    action thread. Stops are recorded when the event stream ends, named by the
    server's ``server_stopping`` announcement or, for an older server, by the
    tray's own request; a vanished stream counts as a crash only once nothing
    listens. Starts are recorded when the stream of a new server process opens,
    once per process. ``phase`` tells a starting server process from a stopping
    one while it does not listen.
    """

    def __init__(
        self,
        journal: ActivityJournal,
        *,
        owns_server: bool,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self._owns_server = owns_server
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._connection = "unknown"
        self._started_at = ""
        self._stopping = False
        self._crashed = False
        self._initiator: str | None = None
        self._expected: str | None = None
        self._tray_start = False

    @property
    def journal(self) -> ActivityJournal:
        return self._journal

    def running_since(self) -> str:
        """When the connected server started (canonical UTC), or empty."""

        with self._lock:
            return self._started_at if self._connection == "connected" else ""

    def phase(self) -> str:
        """``stopping`` after the server's stream ended, else ``starting``.

        Asked while the server process lives without listening.
        """

        with self._lock:
            return "stopping" if self._stopping else "starting"

    # Tray actions, on the tray's action thread.

    def expect_stop(self, initiator: str) -> None:
        """Name the tray's own stop for a server that does not announce it."""

        with self._lock:
            self._expected = initiator

    def expect_start(self) -> None:
        """Attribute the next server start to the tray."""

        with self._lock:
            self._tray_start = True

    def action_failed(self, text: str) -> None:
        with self._lock:
            self._tray_start = False
            self._expected = None
        self._journal.record("action_failed", text, level="error")

    # Monitor observations, on the monitor thread.

    def event_received(self, event: dict[str, Any]) -> None:
        if event.get("type") != "server_stopping":
            return
        payload = event.get("payload")
        initiator = payload.get("initiator") if isinstance(payload, dict) else None
        with self._lock:
            self._initiator = initiator if isinstance(initiator, str) else "unknown"

    def connection_lost(self, close_code: int) -> None:
        with self._lock:
            initiator = self._initiator or self._expected
            self._initiator = self._expected = None
            if not self._owns_server:
                entry = ("disconnected", "Lost the connection to the server", "warning")
            elif initiator is None and close_code not in COOPERATIVE_CLOSE_CODES:
                # A vanished connection proves a crash only once nothing listens.
                self._crashed = self._stopping = True
                return
            else:
                self._stopping = True
                self._tray_start = self._tray_start or initiator == "tray_restart"
                kind = "server_restarting" if initiator in _RESTART_INITIATORS else "server_stopped"
                entry = (kind, _STOP_TEXTS.get(initiator or "", "Server stopped"), "info")
        self._journal.record(entry[0], entry[1], level=entry[2])

    def status_changed(self, status: MonitorStatus) -> None:
        connection = status.connection
        with self._lock:
            previous, self._connection = self._connection, connection
            crashed = self._crashed and connection == "refused"
            if connection in {"refused", "unreachable", "connected"}:
                self._stopping = self._crashed = False
            if connection != "connected":
                entry = self._down_entry(previous, connection, crashed)
            else:
                entry = self._up_entry(previous, status)
                self._started_at = status.started_at
                self._tray_start = False
        if entry is not None:
            kind, text, level, ref = entry
            self._journal.record(kind, text, level=level, ref=ref)

    def _down_entry(
        self, previous: str, connection: str, crashed: bool
    ) -> tuple[str, str, str, str] | None:
        if connection == previous:
            return None
        if crashed:
            return "server_crashed", "Server stopped unexpectedly", "warning", ""
        if not self._owns_server:
            return None
        if connection == "unresponsive":
            return "server_unresponsive", "Server is not responding", "warning", ""
        if connection == "rejected":
            return "port_occupied", "Another application occupies the server's port", "error", ""
        return None

    def _up_entry(self, previous: str, status: MonitorStatus) -> tuple[str, str, str, str] | None:
        if not self._owns_server:
            return "connected", f"Connected to the server at {status.url}", "info", ""
        if status.started_at and status.started_at == self._started_at:
            # The same server process again: a dropped stream, or a busy server.
            if previous == "unresponsive":
                return "server_responding", "Server is responding again", "info", ""
            return None
        ref = f"server:{status.started_at}" if status.started_at else ""
        if ref and self._journal.has(ref):
            return None
        version = f"vBot {status.server_version}" if status.server_version else ""
        if previous == "unknown":
            # The tray's first observation: the server was already running.
            since = f"since {local_time(status.started_at)}" if status.started_at else ""
            return "server_running", f"Server is running{_details(since, version)}", "info", ref
        startup = _duration(status.started_at, self._now())
        origin = " from the tray" if self._tray_start else ""
        took = f"startup {startup}" if startup else ""
        return "server_started", f"Server started{origin}{_details(took, version)}", "info", ref


def local_time(value: str, *, seconds: bool = False) -> str:
    """Render a timestamp in local time: the time alone for today, else with the date."""

    try:
        moment = parse_timestamp(value).astimezone()
    except ValueError:
        return value
    clock = "%H:%M:%S" if seconds else "%H:%M"
    if moment.date() == datetime.now().astimezone().date():
        return moment.strftime(clock)
    return moment.strftime(f"%Y-%m-%d {clock}")


def _details(*parts: str) -> str:
    present = [part for part in parts if part]
    return f" ({', '.join(present)})" if present else ""


def _duration(started_at: str, now: datetime) -> str:
    try:
        elapsed = now - parse_timestamp(started_at)
    except ValueError:
        return ""
    if elapsed < timedelta(0) or elapsed > _MAX_STARTUP:
        return ""
    seconds = round(elapsed.total_seconds())
    return f"{seconds} s" if seconds < 120 else f"{seconds // 60} min {seconds % 60} s"


def _serialize(entry: ActivityEntry) -> str:
    value = {"at": entry.at, "kind": entry.kind, "text": entry.text, "level": entry.level}
    if entry.ref:
        value["ref"] = entry.ref
    return json.dumps(value, ensure_ascii=False) + "\n"


def _load(path: Path) -> tuple[list[ActivityEntry], int]:
    """Read the kept entries and how many lines the journal holds."""

    try:
        with path.open("rb") as journal:
            size = journal.seek(0, os.SEEK_END)
            journal.seek(max(0, size - _READ_LIMIT_BYTES))
            data = journal.read()
    except FileNotFoundError:
        return [], 0
    except OSError as error:
        _LOGGER.warning("Could not read the tray activity journal: %s", error)
        return [], 0
    lines = data.decode("utf-8", errors="replace").splitlines()
    entries: list[ActivityEntry] = []
    for raw in lines:
        with contextlib.suppress(ValueError):
            value = json.loads(raw)
            if not isinstance(value, dict):
                continue
            at, text = value.get("at"), value.get("text")
            if not isinstance(at, str) or not isinstance(text, str):
                continue
            parse_timestamp(at)
            kind, level, ref = value.get("kind"), value.get("level"), value.get("ref")
            entries.append(
                ActivityEntry(
                    at,
                    kind if isinstance(kind, str) else "",
                    text,
                    level if level in _LEVELS else "info",
                    ref if isinstance(ref, str) else "",
                )
            )
    return entries[-KEEP_ENTRIES:], len(lines)
