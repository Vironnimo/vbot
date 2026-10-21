"""Decide which tray toasts the observed application events deserve.

Every toast kind has a boolean ``notifications.<kind>`` setting on the server.
Run toasts wait briefly before they appear: a Run whose Session a vBot window
already shows is acknowledged as read within that hold, and an acknowledgement
arriving later dismisses the toast.

A completed Run's toast says the Agent finished, so it waits for the end of its
Session's work span: the server reports each Session's activity (working,
waiting for Sub-Agents, commands or results that will wake it again, idle), and
the toast shows once that Session's ``session_activity_changed`` reports idle,
naming how long the span took. A later completion in the same span replaces the
held toast, so a span ends with one. Failure toasts never wait for the span.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from cli.application.monitor import COOPERATIVE_CLOSE_CODES, MonitorStatus
from cli.application.state import Operation
from cli.formatting import duration_text

_LOGGER = logging.getLogger("vbot.application.notifications")

NOTIFICATION_KINDS = (
    "run_completed",
    "run_failed",
    "automation_failed",
    "update_result",
    "server_stopped",
)
SERVER_TOAST_KEY = "server"

_RUN_OUTCOMES = {
    "run_completed": "completed",
    "run_failed": "failed",
    "run_interrupted": "interrupted",
}
_FOREGROUND_RUN_KINDS = frozenset({"user", "system"})
_AUTOMATION_RUN_KINDS = {"cron": "Cron"}
_FAILED_UPDATE_PHASES = frozenset({"failed", "rolled_back", "needs_attention"})
_REMEMBERED_RUNS = 256
# A Cron job's failure that no Run reports, such as a failed check.
_CRON_FAILURE_ALERT_EVENT = "cron_failure_alert"
# The server-owned activity of one Session; its hello lists the Sessions that
# are not idle.
_SESSION_ACTIVITY_EVENT = "session_activity_changed"
_BUSY_STATES = frozenset({"working", "waiting"})
# Windows shows at most this many characters of a toast's body.
_TOAST_BODY_MAX_CHARS = 255

Rpc = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
# A Session as Run and activity events name it: (project_id, bare agent_id, session_id).
type SessionKey = tuple[str, str, str]


@dataclass(frozen=True)
class Toast:
    """One notification; a click opens ``session`` or else the status window."""

    key: str
    kind: str
    title: str
    body: str
    failure: bool = False
    session: tuple[str, str] | None = None


class ToastSink(Protocol):
    def show(self, toast: Toast) -> None: ...

    def dismiss(self, key: str) -> None: ...


@dataclass(frozen=True)
class _HeldRun:
    """A completed Run's toast waiting for the end of its Session's work span."""

    run_id: str
    kind: str
    event_type: str
    payload: dict[str, Any]
    # The span's ``work_started_at``; a different one means the span ended unseen.
    span: str


def run_toast_kind(event_type: object, payload: dict[str, Any]) -> str | None:
    """Return the toast kind a terminal Run event deserves, if any.

    Every Cron failure notifies; a Cron Run that completes notifies only when it
    counts as Agent activity (its job delivers to vBot and it had something to
    report). Other background work (Subagents, Channels, reflections, internal
    continuations) never notifies; cancellation is always the user's own action.
    """

    outcome = _RUN_OUTCOMES.get(str(event_type))
    if outcome is None:
        return None
    run_kind = payload.get("run_kind")
    # Protocol field of /ws Run events; present only when false.
    quiet = payload.get("contributes_to_agent_activity") is False
    if run_kind in _AUTOMATION_RUN_KINDS:
        if outcome != "completed":
            return "automation_failed"
        return None if quiet else "run_completed"
    if run_kind not in _FOREGROUND_RUN_KINDS or quiet:
        return None
    return "run_completed" if outcome == "completed" else "run_failed"


class Notifier:
    """Turn monitor and update observations into toasts for one tray."""

    def __init__(
        self,
        sink: ToastSink,
        rpc: Rpc,
        *,
        owns_server: bool,
        update_active: Callable[[], bool],
        hold_seconds: float = 1.5,
    ) -> None:
        self._sink = sink
        self._rpc = rpc
        self._owns_server = owns_server
        self._update_active = update_active
        self._hold_seconds = hold_seconds
        self._lock = threading.Lock()
        self._enabled: dict[str, bool] = {}
        self._acknowledged: OrderedDict[str, None] = OrderedDict()
        self._shown: OrderedDict[str, None] = OrderedDict()
        self._pending: dict[str, asyncio.Task[None]] = {}
        # Session activity, touched only on the monitor's thread: the Sessions the
        # server reports as not idle (with their span start) and the held toasts.
        self._busy: dict[SessionKey, str] = {}
        self._held: dict[SessionKey, _HeldRun] = {}
        self._armed = False
        self._expect_stop = False
        self._server_toast = False

    # Monitor observations, delivered on the monitor's event-loop thread.

    def status_changed(self, status: MonitorStatus) -> None:
        if status.connection == "connected":
            with self._lock:
                self._armed = self._expect_stop = False
                server_toast, self._server_toast = self._server_toast, False
            if server_toast:
                self._sink.dismiss(SERVER_TOAST_KEY)
            self._track(asyncio.get_running_loop().create_task(self._refresh_settings()))
            return
        if status.connection == "unknown":
            return
        with self._lock:
            confirmed = self._armed and status.connection == "refused"
            self._armed = False
            if confirmed and (self._expect_stop or self._update_active()):
                confirmed = False
            if confirmed and self._enabled.get("server_stopped", True):
                self._server_toast = True
            else:
                confirmed = False
        if confirmed:
            self._sink.show(
                Toast(
                    SERVER_TOAST_KEY,
                    "server_stopped",
                    "vBot server stopped",
                    "The server stopped unexpectedly. Start it again from the vBot tray menu.",
                    failure=True,
                )
            )

    def connection_lost(self, close_code: int) -> None:
        with self._lock:
            self._armed = (
                self._owns_server
                and close_code not in COOPERATIVE_CLOSE_CODES
                and not self._expect_stop
            )

    def event_received(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "connection_ready":
            self._connection_ready(event)
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if event_type == _SESSION_ACTIVITY_EVENT:
            self._activity_changed(payload)
            return
        if event_type == "resource_changed":
            scope = payload.get("scope")
            if payload.get("kind") == "sessions" and isinstance(scope, dict):
                run_id = scope.get("read_run_id")
                if isinstance(run_id, str):
                    self._acknowledge(run_id)
            return
        if event_type == _CRON_FAILURE_ALERT_EVENT:
            if isinstance(payload.get("job_id"), str):
                self._track(
                    asyncio.get_running_loop().create_task(self._deliver_job_alert(payload))
                )
            return
        kind = run_toast_kind(event_type, payload)
        run_id = payload.get("run_id")
        if kind is None or not isinstance(run_id, str) or run_id in self._pending:
            return
        with self._lock:
            if run_id in self._acknowledged or run_id in self._shown:
                return
        session = _session_key(payload)
        if kind == "run_completed" and session in self._busy:
            # Finished means finished: the toast waits until the Session is idle.
            self._held[session] = _HeldRun(
                run_id, kind, str(event_type), payload, self._busy[session]
            )
            return
        self._deliver(run_id, kind, str(event_type), payload)

    # Facade observations, delivered from the tray worker thread.

    def expect_server_stop(self) -> None:
        """Announce a stop the tray itself requested; it never raises a toast."""

        with self._lock:
            self._expect_stop = True

    def update_finished(self, operation: Operation, summary: str) -> None:
        with self._lock:
            if not self._enabled.get("update_result", True):
                return
        failed = operation.phase in _FAILED_UPDATE_PHASES
        unchanged = (
            operation.candidate_version is not None
            and operation.candidate_version == operation.previous_version
        )
        title = (
            "vBot update failed"
            if failed
            else "vBot update prepared"
            if operation.phase == "prepared"
            else "vBot is up to date"
            if unchanged
            else "vBot updated"
        )
        body = summary
        if operation.target_label and not failed and not unchanged:
            before = operation.previous_label
            body += (
                f"\nVersion: {before} -> {operation.target_label}"
                if before and operation.phase == "completed"
                else f"\nVersion: {operation.target_label}"
            )
        elif failed and operation.message:
            body += f"\n{operation.message}"
        self._sink.show(Toast(f"update:{operation.id}", "update_result", title, body, failed))

    # Internals.

    def _connection_ready(self, hello: dict[str, Any]) -> None:
        """Start over from the hello's activity: a Session it does not list is idle."""

        if hello.get("replay_status") == "resumed":
            # The replay that follows brings every change since the last event
            # this tray saw, idle reports included, so its activity stays valid.
            return
        rows = hello.get("session_activity")
        self._busy = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("state") in _BUSY_STATES:
                self._busy[_session_key(row)] = _text(row.get("work_started_at"))
        # The idle report may have been lost with the connection, or the server
        # does not report activity: a span that is over releases its toast.
        for session in [key for key in self._held if not self._held_span_runs(key)]:
            self._release(session, None)

    def _activity_changed(self, payload: dict[str, Any]) -> None:
        session = _session_key(payload)
        if payload.get("state") not in _BUSY_STATES:
            self._busy.pop(session, None)
            finished = payload.get("finished_work")
            duration = finished.get("duration_ms") if isinstance(finished, dict) else None
            self._release(session, _milliseconds(duration))
            return
        self._busy[session] = _text(payload.get("work_started_at"))
        if session in self._held and not self._held_span_runs(session):
            self._release(session, None)

    def _held_span_runs(self, session: SessionKey) -> bool:
        """Whether the span a held toast waits for still runs, as far as the tray knows."""

        return session in self._busy and self._busy[session] == self._held[session].span

    def _release(self, session: SessionKey, finished_ms: float | None) -> None:
        held = self._held.pop(session, None)
        if held is not None:
            self._deliver(held.run_id, held.kind, held.event_type, held.payload, finished_ms)

    def _deliver(
        self,
        run_id: str,
        kind: str,
        event_type: str,
        payload: dict[str, Any],
        finished_ms: float | None = None,
    ) -> None:
        if run_id in self._pending:
            return
        task = asyncio.get_running_loop().create_task(
            self._deliver_run(run_id, kind, event_type, payload, finished_ms),
            name=f"run-notification:{run_id}",
        )
        self._pending[run_id] = task
        task.add_done_callback(lambda _task: self._pending.pop(run_id, None))

    def _acknowledge(self, run_id: str) -> None:
        with self._lock:
            self._acknowledged[run_id] = None
            _trim(self._acknowledged)
            shown = run_id in self._shown
        for session, held in list(self._held.items()):
            if held.run_id == run_id:
                del self._held[session]
        task = self._pending.pop(run_id, None)
        if task is not None:
            task.cancel()
        if shown:
            self._sink.dismiss(f"run:{run_id}")

    async def _deliver_run(
        self,
        run_id: str,
        kind: str,
        event_type: str,
        payload: dict[str, Any],
        finished_ms: float | None,
    ) -> None:
        await asyncio.sleep(self._hold_seconds)
        agent_id = str(payload.get("agent_id") or "")
        project_id = payload.get("project_id")
        session_id = str(payload.get("session_id") or "")
        address = f"{agent_id}@{project_id}" if project_id else agent_id
        settings, session, agent = await asyncio.gather(
            self._refresh_settings(),
            self._rpc("session.get", {"agent_id": address, "session_id": session_id}),
            self._rpc("agent.get", {"id": agent_id}) if not project_id else _none(),
        )
        with self._lock:
            if run_id in self._acknowledged or not self._enabled.get(kind, True):
                return
            self._shown[run_id] = None
            _trim(self._shown)
        agent_name = _text(agent.get("name") if agent else None) or agent_id
        row = session.get("session") if session else None
        title = (
            _text(row.get("title")) or _text(row.get("auto_title")) if isinstance(row, dict) else ""
        ) or "New Session"
        self._sink.show(
            Toast(
                f"run:{run_id}",
                kind,
                _run_title(kind, event_type, payload, agent_name),
                _run_body(event_type, payload, title, finished_ms),
                failure=kind != "run_completed",
                session=(address, session_id) if agent_id and session_id else None,
            )
        )

    async def _deliver_job_alert(self, payload: dict[str, Any]) -> None:
        """Show a Cron job's failure alert; it has no Run, so no Session to open."""
        agent_id = str(payload.get("agent_id") or "")
        project_id = payload.get("project_id")
        _settings, agent = await asyncio.gather(
            self._refresh_settings(),
            self._rpc("agent.get", {"id": agent_id}) if agent_id and not project_id else _none(),
        )
        with self._lock:
            if not self._enabled.get("automation_failed", True):
                return
        agent_name = _text(agent.get("name") if agent else None) or agent_id
        body = _text(payload.get("message")) or _text(payload.get("name"))
        if len(body) > _TOAST_BODY_MAX_CHARS:
            body = body[: _TOAST_BODY_MAX_CHARS - 1] + "…"
        self._sink.show(
            Toast(
                f"cron:{payload['job_id']}",
                "automation_failed",
                f"Cron job failed · {agent_name}",
                body,
                failure=True,
            )
        )

    async def _refresh_settings(self) -> None:
        result = await self._rpc("settings.values", {})
        values = result.get("settings") if result else None
        section = values.get("notifications") if isinstance(values, dict) else None
        if not isinstance(section, dict):
            return
        with self._lock:
            self._enabled = {
                kind: section[kind]
                for kind in NOTIFICATION_KINDS
                if isinstance(section.get(kind), bool)
            }

    def _track(self, task: asyncio.Task[None]) -> None:
        task.add_done_callback(_log_failure)


def _run_title(kind: str, event_type: str, payload: dict[str, Any], agent: str) -> str:
    if kind == "automation_failed":
        return f"{_AUTOMATION_RUN_KINDS[str(payload.get('run_kind'))]} Run failed · {agent}"
    if event_type == "run_interrupted":
        return f"{agent} was interrupted"
    return f"{agent} finished" if kind == "run_completed" else f"{agent} failed"


def _run_body(
    event_type: str, payload: dict[str, Any], session_title: str, finished_ms: float | None
) -> str:
    if event_type == "run_interrupted":
        return f"{session_title}\nAutomatic recovery could not finish the Run."
    error = _text(payload.get("error"))
    if event_type == "run_failed" and error:
        first_line = error.splitlines()[0]
        return f"{session_title}\n{first_line[:160]}"
    if finished_ms is not None:
        return f"{session_title}\nFinished after {duration_text(finished_ms)}"
    return session_title


def _session_key(payload: dict[str, Any]) -> SessionKey:
    return (
        _text(payload.get("project_id")),
        _text(payload.get("agent_id")),
        _text(payload.get("session_id")),
    )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _milliseconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _trim(values: OrderedDict[str, None]) -> None:
    while len(values) > _REMEMBERED_RUNS:
        values.popitem(last=False)


async def _none() -> None:
    return None


def _log_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.warning("Tray notification work failed", exc_info=task.exception())
