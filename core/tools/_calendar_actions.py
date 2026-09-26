"""Handle the ``calendar`` Tool's actions: instructions an Agent carries out around an event.

An action belongs to one event and moves with it; its ``when`` counts from the
event's start or end. These handlers add, change and delete actions, relate a
clock-time ``when`` to the event, and render the runs an action has made.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from core.calendar.errors import CalendarEventNotFoundError
from core.projects.address import format_agent_address
from core.tools._calendar_arguments import (
    END_FIELD,
    EVENT_CHANGE_FIELDS,
    OMIT,
    STAND_INS,
    TIMEZONE_FIELD,
    CalendarCallRefusedError,
    is_date,
    minutes_text,
    parse_local,
    refusal,
    render_call,
)
from core.tools._calendar_times import local_text, named_instant, server_zone, unknown_zone
from core.tools._named_zones import named_zone
from core.tools.tools import JsonObject, ToolContext, tool_success

if TYPE_CHECKING:
    from core.calendar import CalendarEvent, CalendarService

_CURRENT_SESSION_WORDS = frozenset({"current", "this", "here", "same", "thissession"})
_UPCOMING_DAYS = 400
_ACTION_REACH = timedelta(days=32)
_LISTED_RUNS = 3


def handle_add_action(
    calendar_service: CalendarService, arguments: JsonObject, context: ToolContext | None
) -> JsonObject:
    event = calendar_service.get_event(str(arguments["id"]))
    missing = [name for name in ("when", "prompt") if name not in arguments]
    if missing:
        texts = {
            "when": '"when", relative to the event such as "start - 1h"',
            "prompt": '"prompt", the instruction the action\'s Run carries out',
        }
        raise CalendarCallRefusedError(
            refusal(
                "add_action needs " + " and ".join(texts[name] for name in missing) + ".",
                arguments,
                **{name: STAND_INS[name] for name in missing},
            )
        )
    when, note = _relative_when(calendar_service, event, arguments)
    target = arguments.get("target")
    if not isinstance(target, str) and context is not None:
        target = format_agent_address(context.agent_id, context.project_id)
    target = str(target)
    action = calendar_service.actions.add(
        event.id,
        when=when,
        prompt=str(arguments["prompt"]),
        target=target,
        session=_session(arguments, context, target),
    )
    notes = [note, _unapplied_note("add_action", arguments, event)]
    return _action_success(calendar_service, action, event, " ".join(filter(None, notes)))


def handle_update_action(
    calendar_service: CalendarService, arguments: JsonObject, context: ToolContext | None
) -> JsonObject:
    action_id = str(arguments["id"])
    current = find_action(calendar_service, action_id)
    event = calendar_service.get_event(current["event_id"])
    fields: dict[str, Any] = {}
    note = None
    if "when" in arguments:
        fields["when"], note = _relative_when(calendar_service, event, arguments)
    if "prompt" in arguments:
        fields["prompt"] = arguments["prompt"]
    if "target" in arguments:
        fields["target"] = arguments["target"]
    if "session" in arguments:
        target = str(fields.get("target", current["target"]))
        fields["session"] = _session(arguments, context, target)
    if not fields:
        event_call = _event_update(arguments, event)
        if event_call is not None:
            raise CalendarCallRefusedError(
                refusal(
                    "update_action changes an action's when, prompt, target or session; the "
                    f"other fields belong to its event {event.title} ({event.id}). To change "
                    "the event:",
                    event_call,
                )
            )
        raise CalendarCallRefusedError(
            refusal(
                "update_action needs a field to change: when, prompt, target or session.",
                arguments,
                when=STAND_INS["when"],
            )
        )
    action = calendar_service.actions.update(action_id, **fields)
    notes = [note, _unapplied_note("update_action", arguments, event)]
    return _action_success(calendar_service, action, event, " ".join(filter(None, notes)))


def handle_delete_action(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    action_id = str(arguments["id"])
    current = find_action(calendar_service, action_id)
    calendar_service.actions.delete(action_id)
    event = calendar_service.get_event(current["event_id"])
    data: JsonObject = {
        "id": action_id,
        "event": f"{event.title} ({event.id})",
        "status": "deleted",
    }
    unapplied = _unapplied_note("delete_action", arguments, event)
    if unapplied:
        data["note"] = unapplied
    return tool_success(data)


def _event_update(arguments: JsonObject, event: CalendarEvent) -> JsonObject | None:
    """The update call for the event fields an action call carried, or None if there are none.

    A title that finds the event by its title only names it, so it asks for no change.
    """
    names = [
        name
        for name in EVENT_CHANGE_FIELDS
        if name in arguments and not (name == "title" and _names_event(arguments[name], event))
    ]
    if not names:
        return None
    call: JsonObject = {"action": "update", "id": event.id}
    call.update({name: arguments[name] for name in names})
    if TIMEZONE_FIELD in arguments and ("start" in names or END_FIELD in names):
        # Those times were written in the named zone.
        call[TIMEZONE_FIELD] = arguments[TIMEZONE_FIELD]
    return call


def _unapplied_note(action: str, arguments: JsonObject, event: CalendarEvent) -> str | None:
    """Name the event fields an action call carried but did not apply, with the call that does."""
    call = _event_update(arguments, event)
    if call is None:
        return None
    names = [name for name in call if name not in {"action", "id", TIMEZONE_FIELD}]
    verb = "was" if len(names) == 1 else "were"
    return (
        f"{', '.join(names)} {verb} not applied: {action} works on the action, not on its "
        f"event. To change the event, send {render_call(call)}."
    )


def _names_event(title: Any, event: CalendarEvent) -> bool:
    """Whether ``title`` is how a call finds this event by its title."""
    wanted = str(title).strip().casefold()
    return bool(wanted) and wanted in event.title.casefold()


def find_action(calendar_service: CalendarService, action_id: str) -> dict[str, Any]:
    for item in calendar_service.actions.list_actions():
        if item["id"] == action_id:
            return item
    raise CalendarEventNotFoundError(f"Calendar action not found: {action_id}")


def _relative_when(
    calendar_service: CalendarService, event: CalendarEvent, arguments: JsonObject
) -> tuple[str, str | None]:
    """Keep a relative when; turn a clock time for a single event into one."""
    when = str(arguments["when"])
    moment = parse_local(when)
    if moment is None and not is_date(when):
        return when, None
    server = server_zone(calendar_service)
    zone: tzinfo = server
    name = arguments.get(TIMEZONE_FIELD)
    if isinstance(name, str):
        found = named_zone(name)
        if found is None:
            raise CalendarCallRefusedError(unknown_zone(name, server, arguments))
        zone = found
    if moment is None:
        moment = datetime.combine(date.fromisoformat(when), time.min)
    if moment.tzinfo is None and isinstance(name, str):
        moment = named_instant(arguments, "when", moment, name, zone)
    elif moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    start = _occurrence_start_near(calendar_service, event, moment)
    minutes = round((moment - start).total_seconds() / 60)
    if minutes == 0:
        relative = "start"
    else:
        relative = f"start {'-' if minutes < 0 else '+'} {minutes_text(abs(minutes))}"
    if event.rrule is not None:
        raise CalendarCallRefusedError(
            refusal(
                f'"{when}" is a clock time, but an action repeats with every occurrence of this '
                f'event, so its when counts from the event. At the nearest occurrence, "{when}" '
                f'is "{relative}".',
                arguments,
                when=relative,
                timezone=OMIT,
            )
        )
    return relative, f'Read "{when}" as "{relative}".'


def _occurrence_start_near(
    calendar_service: CalendarService, event: CalendarEvent, near: datetime
) -> datetime:
    """The start of the occurrence closest to ``near``, as an aware moment."""
    server = server_zone(calendar_service)
    occurrences = calendar_service.event_occurrences(
        event, (near - _ACTION_REACH).astimezone(UTC), (near + _ACTION_REACH).astimezone(UTC)
    )
    starts = [
        item.start_utc
        if item.start_utc is not None
        else datetime.combine(item.start_date or date.min, time.min, server)
        for item in occurrences
    ]
    if starts:
        return min(starts, key=lambda item: abs((item - near).total_seconds()))
    if event.all_day and event.start_date:
        return datetime.combine(date.fromisoformat(event.start_date), time.min, server)
    return calendar_service.event_span(event)[0]


def _session(arguments: JsonObject, context: ToolContext | None, target: str) -> str | None:
    session = arguments.get("session")
    if not isinstance(session, str):
        return None
    if session.strip().casefold().replace(" ", "") not in _CURRENT_SESSION_WORDS:
        return session
    if context is None:
        return None
    own = format_agent_address(context.agent_id, context.project_id)
    if target != own:
        raise CalendarCallRefusedError(
            refusal(
                f"the current Session belongs to {own}, not to the target {target}. Omit "
                "session for a fresh Session each time.",
                arguments,
                session=OMIT,
            )
        )
    return context.session_id


def _action_success(
    calendar_service: CalendarService,
    action: dict[str, Any],
    event: CalendarEvent,
    note: str | None,
) -> JsonObject:
    zone = server_zone(calendar_service)
    data: JsonObject = {
        "id": action["id"],
        "event": f"{event.title} ({event.id})",
        "when": action["when"],
        "target": action["target"],
        "session": action.get("session") or "a fresh Session each time",
    }
    notes = [note] if note else []
    due = _next_due(calendar_service, action, event)
    if due is None:
        notes.append("No occurrence of the event lies ahead, so the action will not run.")
    else:
        data["next_due"] = local_text(due, zone)
    if notes:
        data["note"] = " ".join(notes)
    return tool_success(data)


def _next_due(
    calendar_service: CalendarService, action: dict[str, Any], event: CalendarEvent
) -> datetime | None:
    now = datetime.now(UTC)
    if event.all_day and event.start_date:
        first = datetime.combine(
            date.fromisoformat(event.start_date), time.min, server_zone(calendar_service)
        )
    else:
        first = calendar_service.event_span(event)[0]
    # From now, or from the first occurrence of an event that lies further ahead.
    begin = max(now - _ACTION_REACH, first.astimezone(UTC) - timedelta(days=1))
    occurrences = calendar_service.event_occurrences(
        event, begin, begin + timedelta(days=_UPCOMING_DAYS)
    )
    dues = [
        _instant(row["scheduled_at"])
        for row in calendar_service.actions.project(occurrences)
        if row["action_id"] == action["id"]
        and row["status"] == "pending"
        and _instant(row["expires_at"]) > now
    ]
    return min(dues) if dues else None


def action_lines(action: dict[str, Any], rows: list[dict[str, Any]], zone: ZoneInfo) -> list[str]:
    """One action as list lines: when, target, prompt, recent runs and the next one."""
    session = action.get("session")
    where = f"in Session {session}" if session else "in a fresh Session"
    lines = [f"action {action['id']}: {action['when']}, runs {action['target']} {where}"]
    lines.append("  prompt: " + "\n    ".join(str(action["prompt"]).splitlines()))
    now = datetime.now(UTC)
    done: list[str] = []
    upcoming = None
    for row in sorted(rows, key=lambda item: _instant(item["scheduled_at"])):
        due = local_text(_instant(row["scheduled_at"]), zone)
        if row["status"] == "pending":
            if upcoming is None and _instant(row["expires_at"]) > now:
                upcoming = due
            continue
        detail = f", Session {row['session']}" if row.get("session") else ""
        done.append(f"  {due} {row['status']}{detail}")
    if len(done) > _LISTED_RUNS:
        lines.append(f"  {len(done) - _LISTED_RUNS} earlier runs in this window")
    lines.extend(done[-_LISTED_RUNS:])
    if upcoming:
        lines.append(f"  next: {upcoming}")
    return lines


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)


__all__ = [
    "action_lines",
    "find_action",
    "handle_add_action",
    "handle_delete_action",
    "handle_update_action",
]
