"""Event-owned actions: persistence, relative scheduling, and durable execution history."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from core.calendar.errors import (
    CalendarEventNotFoundError,
    CalendarStorageError,
    CalendarValidationError,
)
from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    child_path,
    error_diagnostic,
    load_validated_json_file,
    validate_json_file,
    validate_required_fields,
)
from core.json_documents import (
    JsonDocumentFormat,
    JsonDocumentWriteError,
    json_document,
    json_list,
    json_map,
    json_object,
    strip_unknown_fields,
    validate_format_version,
    warn_unknown_fields,
    write_json_document,
)
from core.projects.address import parse_agent_address
from core.runs import RunKind
from core.sessions import SessionAddress
from core.utils.ids import new_id
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation import TriggerService
    from core.calendar.service import CalendarEvent, CalendarService, EventOccurrence
    from core.projects import AgentResolver
    from core.runs import Run
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("calendar.actions")
_WHEN = re.compile(r"^(start|end)(?:\s*([+-])\s*([1-9][0-9]*)\s*([mhd]))?$")
_TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted", "missed"})
_MAX_OFFSET = 31 * 24 * 60
_MAX_ACTIONS = 16
# Finished history is kept this long after expiry, then pruned once the scan
# window no longer reaches its occurrence (it can then never become due again).
_RETENTION = timedelta(days=30)
_EXECUTION_STATUSES = _TERMINAL | {"pending", "claimed", "running"}

CALENDAR_ACTIONS_FORMAT_VERSION = 1
_ACTION_FIELDS = frozenset(
    ("id", "event_id", "when", "prompt", "target", "session", "created_at", "scanned_until")
)
_EXECUTION_FIELDS = frozenset(
    (
        "id",
        "action_id",
        "event_id",
        "occurrence_start",
        "scheduled_at",
        "expires_at",
        "target",
        "session",
        "run_id",
        "status",
    )
)
CALENDAR_ACTIONS_SHAPE = json_document(
    {"actions", "executions"},
    {
        "actions": json_list(json_object(_ACTION_FIELDS), key="id"),
        "executions": json_map(json_object(_EXECUTION_FIELDS)),
    },
)


def parse_action_when(value: object) -> tuple[str, int, str]:
    """Return anchor, signed minutes, and a canonical user-visible expression."""
    match = _WHEN.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise CalendarValidationError(
            "when must be start or end, optionally + or - a duration such as 'start - 1h'"
        )
    anchor, sign, count, unit = match.groups()
    minutes = int(count or 0) * {None: 1, "m": 1, "h": 60, "d": 1440}[unit]
    if minutes > _MAX_OFFSET:
        raise CalendarValidationError("when offset must not exceed 31 days")
    return (
        anchor,
        -minutes if sign == "-" else minutes,
        f"{anchor} {sign} {count}{unit}" if sign else anchor,
    )


def validate_calendar_actions_file(actions_path: str | Path) -> JsonValidationReport:
    """Validate persisted ``calendar/actions.json`` without consuming it."""
    return validate_json_file(actions_path, validate_calendar_actions_data, missing_ok=True)


def validate_calendar_actions_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``calendar/actions.json`` document.

    Actions and their execution history load as a whole: any error disables the
    action store, and a document that fails to load is never overwritten.
    """
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]
    if not validate_format_version(diagnostics, data, CALENDAR_ACTIONS_FORMAT_VERSION):
        return diagnostics
    warn_unknown_fields(
        diagnostics, "$", data, CALENDAR_ACTIONS_SHAPE, label="calendar action field"
    )
    validate_required_fields(diagnostics, "$", data, frozenset(("actions", "executions")))
    actions = data.get("actions", [])
    if not isinstance(actions, list):
        add_error(diagnostics, "$.actions", "must be an array")
        actions = []
    action_ids: set[str] = set()
    for index, action in enumerate(actions):
        path = f"$.actions[{index}]"
        try:
            _validate_action_record(action)
        except (CalendarValidationError, ValueError) as error:
            add_error(diagnostics, path, str(error))
            continue
        if action["id"] in action_ids:
            add_error(diagnostics, f"{path}.id", f"duplicate action id: {action['id']}")
        action_ids.add(action["id"])
    executions = data.get("executions", {})
    if not isinstance(executions, dict):
        add_error(diagnostics, "$.executions", "must be an object")
        executions = {}
    for key, row in executions.items():
        try:
            _validate_execution_record(key, row)
        except ValueError as error:
            add_error(diagnostics, child_path("$.executions", key), str(error))
    return diagnostics


CALENDAR_ACTIONS_FORMAT = JsonDocumentFormat(
    name="Calendar actions",
    version=CALENDAR_ACTIONS_FORMAT_VERSION,
    shape=CALENDAR_ACTIONS_SHAPE,
    validate=validate_calendar_actions_data,
)


def _validate_action_record(action: Any) -> None:
    """Check the stored fields of one action, without resolving its target."""
    if not isinstance(action, dict):
        raise CalendarValidationError("action must be an object")
    for field in ("id", "event_id", "target", "prompt", "created_at", "scanned_until"):
        if not isinstance(action.get(field), str) or not action[field].strip():
            raise CalendarValidationError(f"{field} must be a non-empty string")
    if len(action["prompt"]) > 10000:
        raise CalendarValidationError("prompt must not exceed 10000 characters")
    parse_action_when(action.get("when"))
    _instant(action["created_at"])
    _instant(action["scanned_until"])
    parse_agent_address(action["target"])
    session = action.get("session")
    if session is not None and (not isinstance(session, str) or not session.strip()):
        raise CalendarValidationError("session must be a non-empty string or null")


def _validate_execution_record(key: str, row: Any) -> None:
    if not isinstance(row, dict):
        raise ValueError("execution must be an object")
    if row.get("status") not in _EXECUTION_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(_EXECUTION_STATUSES))}")
    if row.get("id") != key:
        raise ValueError("id must equal the execution key")
    for field in ("action_id", "event_id", "occurrence_start", "target"):
        if not isinstance(row.get(field), str):
            raise ValueError(f"{field} must be a string")
    parse_agent_address(row["target"])
    for field in ("scheduled_at", "expires_at"):
        if not isinstance(row.get(field), str):
            raise ValueError(f"{field} must be a timestamp string")
        _instant(row[field])


class CalendarActions:
    """Own action CRUD and scheduling; CalendarService remains the occurrence authority.

    Claims persist before admission. An uncertain admission after process death is
    never retried. Pending work can be recomputed from current event definitions.
    """

    def __init__(self, calendar: CalendarService, data_root: Path) -> None:
        self._calendar = calendar
        self._path = data_root / "calendar" / "actions.json"
        self._actions: dict[str, dict[str, Any]] = {}
        self._executions: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._storage_error: CalendarStorageError | None = None
        self._trigger: TriggerService | None = None
        self._resolver: AgentResolver | None = None
        self._sessions: ChatSessionManager | None = None
        self._task: asyncio.Task[None] | None = None
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._runs: dict[str, Run] = {}
        self._changed = asyncio.Event()
        self._sleep_seconds = 30.0
        self._recovery_pending: set[str] = set()
        self._calendar.add_changed_callback(self._wake)

    def configure(
        self, trigger: TriggerService, resolver: AgentResolver, sessions: ChatSessionManager
    ) -> None:
        self._trigger, self._resolver, self._sessions = trigger, resolver, sessions

    def _wake(self) -> None:
        self._changed.set()
        # Withdraw work still awaiting admission; active Runs retain their history.
        for key, task in tuple(self._workers.items()):
            if key not in self._runs:
                task.cancel()

    def _load(self) -> None:
        if not self._loaded:
            try:
                data = load_validated_json_file(
                    self._path, validate_calendar_actions_data, missing_ok=True
                )
            except (OSError, JsonConfigValidationError) as error:
                self._storage_error = CalendarStorageError(f"Cannot load calendar actions: {error}")
                _LOGGER.error("Calendar action storage is unavailable: %s", error)
            else:
                if data is not None:
                    self._adopt(strip_unknown_fields(data, CALENDAR_ACTIONS_SHAPE))
            self._loaded = True
        if self._storage_error is not None:
            raise self._storage_error

    def _adopt(self, data: dict[str, Any]) -> None:
        """Take over a validated document's modeled fields as the in-memory store."""
        for action in data["actions"]:
            self._actions[action["id"]] = action
        for key, row in data["executions"].items():
            if row["status"] in {"claimed", "running"}:
                row["status"] = "interrupted"
                self._recovery_pending.add(key)
            self._executions[key] = row

    def _save(self) -> None:
        """Write actions and history, keeping the unknown fields of the file on disk."""
        payload = {"actions": list(self._actions.values()), "executions": self._executions}
        try:
            write_json_document(self._path, payload, CALENDAR_ACTIONS_FORMAT)
        except JsonDocumentWriteError as error:
            raise CalendarStorageError(str(error)) from error
        except OSError as error:
            raise CalendarStorageError(f"Cannot save calendar actions: {error}") from error

    def _validate(self, action: dict[str, Any], *, references: bool = True) -> None:
        _validate_action_record(action)
        agent, project = parse_agent_address(action["target"])
        session = action.get("session")
        if references and self._resolver is not None:
            try:
                self._resolver.resolve_agent(project, agent)
            except Exception as error:
                raise CalendarValidationError(
                    "target does not identify an available agent"
                ) from error
        if (
            references
            and session
            and self._sessions is not None
            and not self._sessions.exists(
                SessionAddress(project_id=project, agent_id=agent, session_id=session)
            )
        ):
            raise CalendarValidationError("session does not exist for the selected target")

    def add(
        self,
        event_id: str,
        *,
        when: str,
        prompt: str,
        target: str,
        session: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self._load()
        self._calendar.get_event(event_id)
        if sum(a["event_id"] == event_id for a in self._actions.values()) >= _MAX_ACTIONS:
            raise CalendarValidationError("A calendar event supports at most 16 actions")
        if len(self._actions) >= 512:
            raise CalendarValidationError(
                "The calendar supports at most 512 actions; remove unused actions first"
            )
        stamp = (now or datetime.now(UTC)).isoformat()
        action: dict[str, Any] = {
            "id": new_id("act", claim=lambda candidate: candidate not in self._actions),
            "event_id": event_id,
            "when": parse_action_when(when)[2],
            "prompt": prompt,
            "target": target,
            "session": session,
            "created_at": stamp,
            "scanned_until": stamp,
        }
        self._validate(action)
        self._actions[action["id"]] = action
        try:
            self._save()
        except Exception:
            self._actions.pop(action["id"])
            raise
        self._calendar._notify_changed()
        _LOGGER.info("Calendar action added (event=%s action=%s)", event_id, action["id"])
        return self._payload(action)

    def update(self, action_id: str, **fields: Any) -> dict[str, Any]:
        self._load()
        previous = self._get(action_id)
        if not fields or set(fields) - {"when", "prompt", "target", "session"}:
            raise CalendarValidationError("update_action requires when, prompt, target, or session")
        action = {**previous, **fields}
        action["when"] = parse_action_when(action["when"])[2]
        self._validate(action)
        self._actions[action_id] = action
        try:
            self._save()
        except Exception:
            self._actions[action_id] = previous
            raise
        self._calendar._notify_changed()
        _LOGGER.info("Calendar action updated (action=%s)", action_id)
        return self._payload(action)

    def delete(self, action_id: str) -> None:
        self._load()
        previous = self._get(action_id)
        del self._actions[action_id]
        try:
            self._save()
        except Exception:
            self._actions[action_id] = previous
            raise
        self._calendar._notify_changed()
        _LOGGER.info("Calendar action deleted (action=%s)", action_id)

    def _get(self, action_id: str) -> dict[str, Any]:
        if action_id not in self._actions:
            raise CalendarEventNotFoundError(f"Calendar action not found: {action_id}")
        return self._actions[action_id]

    def _payload(self, action: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in action.items()
            if key not in {"created_at", "scanned_until"}
        }

    def retarget_identity(self, source: str, destination: str) -> None:
        """Retarget under the caller's Agent rename transaction; support compensation."""
        self._load()
        previous = copy.deepcopy((self._actions, self._executions))
        for action in self._actions.values():
            if action["target"] == source:
                action["target"] = destination
        for row in self._executions.values():
            if row["target"] == source:
                row["target"] = destination
        try:
            self._save()
        except Exception:
            self._actions, self._executions = previous
            raise
        self._calendar._notify_changed()

    def list_actions(self, event_id: str | None = None) -> list[dict[str, Any]]:
        try:
            self._load()
        except CalendarStorageError:
            return []
        live = {event.id for event in self._calendar.list_events()}
        return [
            self._payload(a)
            for a in self._actions.values()
            if a["event_id"] in live and (event_id is None or a["event_id"] == event_id)
        ]

    def project(self, occurrences: list[EventOccurrence]) -> list[dict[str, Any]]:
        try:
            self._load()
        except CalendarStorageError:
            return []
        result = []
        actions = self.list_actions()
        for occurrence in occurrences:
            event = self._calendar.get_event(occurrence.event_id)
            for action in actions:
                if action["event_id"] == event.id:
                    key, row = self._execution(action, event, occurrence)
                    previous = self._executions.get(key)
                    if previous and self._consumed(previous, row):
                        row = previous
                    elif _instant(row["expires_at"]) <= datetime.now(UTC) - _RETENTION:
                        continue  # History beyond retention is no longer known.
                    elif _instant(row["expires_at"]) <= datetime.now(UTC):
                        row["status"] = "missed"
                    result.append(copy.deepcopy(row))
        return result

    @property
    def storage_error(self) -> str | None:
        return str(self._storage_error) if self._storage_error is not None else None

    @staticmethod
    def _consumed(previous: dict[str, Any], row: dict[str, Any]) -> bool:
        # Claims consume a scheduled instant, not the slot forever. Compare UTC
        # instants rather than display anchors: a timezone-only change must not rearm.
        if previous["status"] == "pending":
            return False
        return _instant(previous["scheduled_at"]) == _instant(row["scheduled_at"])

    def _execution(
        self, action: dict[str, Any], event: CalendarEvent, occurrence: EventOccurrence
    ) -> tuple[str, dict[str, Any]]:
        zone = ZoneInfo(self._calendar.system_timezone_name())
        start, end = occurrence.start_utc, occurrence.end_utc
        if occurrence.all_day:
            assert occurrence.start_date is not None and occurrence.end_date is not None
            start = datetime.combine(occurrence.start_date, time.min, zone).astimezone(UTC)
            end = datetime.combine(occurrence.end_date, time.min, zone).astimezone(UTC)
        assert start is not None and end is not None
        anchor, minutes, _ = parse_action_when(action["when"])
        due = (start if anchor == "start" else end) + timedelta(minutes=minutes)
        expires = start if due < start else end if due < end else due + timedelta(hours=1)
        key = f"{action['id']}:{occurrence.occurrence_start if event.rrule else 'single'}"
        return key, {
            "id": key,
            "action_id": action["id"],
            "event_id": event.id,
            "occurrence_start": occurrence.occurrence_start,
            "scheduled_at": due.isoformat(),
            "expires_at": expires.isoformat(),
            "target": action["target"],
            "session": action.get("session"),
            "run_id": None,
            "status": "pending",
        }

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            self._load()
        except CalendarStorageError:
            return
        self._task = asyncio.create_task(self._schedule(), name="calendar-actions")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        for worker in self._workers.values():
            worker.cancel()

    async def aclose(self) -> None:
        tasks = [*self._workers.values(), *([self._task] if self._task else [])]
        self.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None

    async def _schedule(self) -> None:
        while True:
            self._changed.clear()
            try:
                await self.tick()
            except CalendarStorageError:
                _LOGGER.exception("Calendar action scheduling paused after storage failure")
            except Exception:
                _LOGGER.exception("Calendar action scheduling failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout=self._sleep_seconds)

    async def tick(self, now: datetime | None = None) -> None:
        """Reconcile due occurrences and start bounded work; injectable time for tests."""
        self._load()
        now = now or datetime.now(UTC)
        self._sleep_seconds = 30.0
        self._calendar._ensure_events_loaded()
        self._recover()
        live = {event.id: event for event in self._calendar.list_events()}
        desired: dict[
            str, tuple[dict[str, Any], dict[str, Any], CalendarEvent, EventOccurrence]
        ] = {}
        seen: set[str] = set()
        changed = False
        for action_id, action in list(self._actions.items()):
            if action["event_id"] not in live and not self._calendar._invalid_event_entries:
                del self._actions[action_id]
                changed = True
        for action in self._actions.values():
            event = live.get(action["event_id"])
            if event is None:
                continue
            anchor, offset, _ = parse_action_when(action["when"])
            duration = timedelta(days=event.duration_days or 0, minutes=event.duration_minutes or 0)
            shift = timedelta(minutes=offset) + (duration if anchor == "end" else timedelta())
            # Include the previous scan boundary and overlapping long events.
            lower = min(_instant(action["scanned_until"]), now) - shift - timedelta(days=1)
            upper = now - shift + timedelta(days=1)
            while lower < upper:
                edge = min(lower + timedelta(days=60), upper)
                occurrences = self._calendar.event_occurrences(event, lower, edge)
                for occurrence in occurrences:
                    key, row = self._execution(action, event, occurrence)
                    seen.add(key)
                    due, expires = _instant(row["scheduled_at"]), _instant(row["expires_at"])
                    if due > now:
                        self._sleep_seconds = min(
                            self._sleep_seconds, max(0.05, (due - now).total_seconds())
                        )
                    if expires <= _instant(action["created_at"]) or due > now:
                        continue
                    previous = self._executions.get(key)
                    if previous and self._consumed(previous, row):
                        continue
                    # An admitted Run keeps its row until completion even when
                    # the event moves. Reconcile the new occurrence on the next tick.
                    if key in self._workers:
                        continue
                    if expires <= now:
                        row["status"] = "missed"
                    if previous != row:
                        self._executions[key] = row
                        changed = True
                    # Workers mutate the stored row, never a recomputed copy: an
                    # equal pending row already stored stays the single source of truth.
                    stored = self._executions[key]
                    if stored["status"] == "pending":
                        desired[key] = (stored, action, event, occurrence)
                lower = edge
            if now - _instant(action["scanned_until"]) >= timedelta(minutes=1):
                action["scanned_until"] = now.isoformat()
                changed = True
        for key, row in list(self._executions.items()):
            if key not in self._workers and self._prunable(key, row, desired, seen, now):
                del self._executions[key]
                changed = True
        if changed:
            self._save()
        for key, values in desired.items():
            if key in self._workers or len(self._workers) >= 4 or self._trigger is None:
                continue
            row, action, event, occurrence = values
            self._workers[key] = asyncio.create_task(
                self._execute(key, row, copy.deepcopy(action), event, occurrence),
                name=f"calendar-action:{action['id']}",
            )
            self._workers[key].add_done_callback(partial(self._worker_done, key))
        if changed:
            # Invalidation must not withdraw our own pending work.
            self._calendar._notify_action_changed()

    def _prunable(
        self, key: str, row: dict[str, Any], desired: dict[str, Any], seen: set[str], now: datetime
    ) -> bool:
        if row["status"] == "pending":
            return key not in desired
        if row["action_id"] not in self._actions:
            return True  # Action ids are never reused, so its history cannot refire.
        # The scan recomputes every occurrence it still reaches; retaining those rows
        # keeps them consumed. Expired rows it no longer reaches can never become due.
        return (
            row["status"] in _TERMINAL
            and key not in seen
            and _instant(row["expires_at"]) <= now - _RETENTION
        )

    def _worker_done(self, key: str, task: asyncio.Task[None]) -> None:
        if self._workers.get(key) is task:
            self._workers.pop(key)
        self._changed.set()
        if not task.cancelled() and task.exception() is not None:
            error = task.exception()
            _LOGGER.error("Calendar action worker failed", exc_info=error)

    def _recover(self) -> None:
        if not self._recovery_pending or self._sessions is None:
            return
        for key in self._recovery_pending:
            row = self._executions[key]
            if row.get("session") and row.get("run_id"):
                agent, project = parse_agent_address(row["target"])
                address = SessionAddress(
                    project_id=project, agent_id=agent, session_id=row["session"]
                )
                if self._sessions.exists(address):
                    summary = self._sessions.get(address).find_run_summary(run_id=row["run_id"])
                    if summary is not None and summary.status in _TERMINAL:
                        row["status"] = summary.status
        self._save()
        self._recovery_pending.clear()

    async def _execute(
        self,
        key: str,
        row: dict[str, Any],
        action: dict[str, Any],
        event: CalendarEvent,
        occurrence: EventOccurrence,
    ) -> None:
        run: Run | None = None
        input_persisted = False

        def admitted() -> None:
            nonlocal input_persisted
            input_persisted = True

        def mark(**fields: Any) -> None:
            # Always record progress in the stored row, even if a compensating
            # rollback replaced the execution map while this worker was waiting.
            self._executions.setdefault(key, row).update(fields)

        try:
            assert self._trigger is not None
            self._validate(action)
            remaining = (_instant(row["expires_at"]) - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                mark(status="missed")
                return
            # The claim precedes any await that can admit work.
            mark(status="claimed")
            self._save()
            agent, project = parse_agent_address(action["target"])
            message = json.dumps(
                {
                    "instruction": action["prompt"],
                    "calendar_event": {
                        "id": event.id,
                        "title": event.title,
                        "notes": event.notes,
                        "start": occurrence.occurrence_start,
                        "end": occurrence.occurrence_end
                        or (occurrence.end_date.isoformat() if occurrence.end_date else None),
                        "timezone": event.tz_name or self._calendar.system_timezone_name(),
                    },
                },
                ensure_ascii=False,
            )
            async with asyncio.timeout(remaining):
                run = await self._trigger.trigger_run(
                    agent,
                    message,
                    action.get("session"),
                    project_id=project,
                    run_kind=RunKind.CALENDAR,
                    input_persisted_hook=admitted,
                )
            self._runs[key] = run
            mark(status="running", run_id=run.id, session=run.session_id)
            try:
                self._save()
            except CalendarStorageError:
                _LOGGER.exception("Cannot persist admitted calendar Run (action=%s)", action["id"])
            self._calendar._notify_action_changed()
            await run.wait()
            mark(status="completed")
        except TimeoutError:
            mark(
                status="failed"
                if run is not None
                else "interrupted"
                if input_persisted
                else "missed"
            )
        except asyncio.CancelledError:
            mark(status="interrupted" if run is not None or input_persisted else "pending")
            raise
        except Exception:
            if run is None:
                mark(status="failed")
                _LOGGER.exception("Calendar action admission failed (action=%s)", action["id"])
            else:
                mark(status=run.status.value)
        finally:
            self._runs.pop(key, None)
            self._workers.pop(key, None)
            self._save()
            self._calendar._notify_action_changed()


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires an explicit timezone")
    return parsed.astimezone(UTC)
