# Calendar

Local-first calendar: a persisted event store with iCalendar (RFC 5545) semantics, a read-only cron projection, a WebUI tab, and exactly one agent tool.

## Overview

`core/calendar/` owns calendar events end to end: storage, recurrence expansion, single-occurrence exclusion, and free-slot search. `core/automation` projects cron jobs into windows on the fly (nothing persisted, never written anywhere else). The WebUI tab and the `calendar` tool are consumers of the same service; the server publishes invalidation events so every accessor refreshes when anything mutates the store.

Not owned here: scheduling itself (Automation owns cron), application timezone configuration (Settings owns it), external calendar sync. CalDAV is a future Extension (user decision 2026-08-27); the store's standard iCalendar semantics exist so that extension becomes a thin adapter. The user explicitly removed `location` and any timezone parameter from the agent surface - events anchor in the configured application timezone.

## Terms

Core terms (Run, Session, Tool) live in `.vorch/GLOSSARY.md`.

### Occurrence
**Definition:** One expanded instance of an event inside a query window; produced on the fly by expansion, never persisted. A recurring event yields many occurrences; a single event at most one.
**Not:** The persisted `CalendarEvent` record.

### EXDATE (single-occurrence exclusion)
**Definition:** RFC 5545 exception that removes one occurrence from a repeating event while keeping the series. Added via `CalendarService.add_exdate`; valid only on recurring events.
**Not:** Deleting the event.

### occurrence_start
**Definition:** An occurrence's start rendered in the event's own anchor form - a naive local datetime for timed events, a plain date for all-day events. This is the value agents echo back to remove one occurrence (tool `delete` + `start`) and the exact EXDATE form.
**Not:** The UTC instant (`start_utc`) the UI renders from.

### when expression
**Definition:** The agent-facing window grammar (`today`, `this week`, `next month`, a date, a year-month, `start..end`) parsed by `core/calendar/when.py` against the server zone and current time, so models never do date arithmetic.
**Not:** An ISO window; the RPC `calendar.window` surface uses explicit `from`/`to` bounds instead.

## Data Model

- Storage: `<data_dir>/calendar/events.json`, a JSON array written atomically. Invalid entries are preserved and skipped on load; if the file is unreadable the service degrades (reads return empty, mutations raise) rather than destroying data.
- `CalendarEvent` carries exactly one start shape, enforced by validation: `start_utc` for single timed events (absolute instant), `start_local` + `tz_name` for recurring timed events (wall-clock anchor; `tz_name` is always the server zone), `start_date` for all-day events. `exdates` exist only on recurring events.
- Caps (module constants in `service.py`): 2000 events, 62-day window span, 500 occurrences per event per query, 1000 exdates per event, title 200 / notes 5000 chars.

## Interfaces

- `CalendarService` (`core/calendar/service.py`): `create_event`, `update_event` (roundtrips the stored event through the create shape; omitted fields keep), `delete_event`, `add_exdate`, `occurrences_in_window` (half-open UTC window), `find_free_slots` (timed events block their span, all-day events block whole local days, slots start no earlier than now, 5-minute alignment, max 5), `parse_window` / `resolve_when`, and the live `set_timezone` seam owned by Runtime Settings application.
- RPC (`server/rpc/calendar_methods.py`): `calendar.window` returns `occurrences` + `events` + `cron` + `system_timezone`; `calendar.create/update/delete` and `calendar.add_exdate` (params `id` + `occurrence_start`) publish `RESOURCE_KIND_CALENDAR` invalidations. Mutations from any accessor (including the agent tool) reach the UI through this invalidation path.
- Agent tool `calendar` (`core/tools/calendar.py`): one flat action tool (`list`, `create`, `update`, `delete`, `find_free`) with 8 parameters (`action`, `when`, `id`, `title`, `start`, `duration`, `rrule`, `notes`); only `action` is required, the handler validates per-action fields and rejects action-foreign fields with a recommendation. All agent-facing times are server-local naive ISO strings (or plain dates). Its detailed contract lives in the per-tool spec (see References).
- Cron projection: `CronService.project_occurrences(window_start, window_end)` returns `CronOccurrence` dataclasses (read-only; respects `remaining_runs` budget, capped per job). The calendar tool's `list`/`find_free` ignore cron jobs - cron is a UI layer only.

## Conventions

- Recurring timed events anchor wall-clock in the server timezone (09:00 stays 09:00 across DST; test-verified over the Berlin transition). Single timed events persist as UTC instants; all-day events are dates in the server zone. Clearing a recurrence re-resolves the event as a single timed event from its wall-clock start and drops any exdates (exceptions are meaningless on a single event).
- The application zone is injected at construction (`CalendarService(data_root, tz=...)`) from `server.timezone`, defaults to the host zone when the setting is absent, and changes live through `set_timezone`; tests pass `tz="Europe/Berlin"` explicitly for determinism. Never call `tzlocal` per operation.
- The tool layer maps `duration` to `duration_minutes`/`duration_days` by the event's kind (the start form decides: date = all-day, datetime = timed) and passes `all_day` explicitly on update so kind switches work without exposing an `all_day` parameter.
- Window bounds are inclusive days: a date bound selects its whole local day (`to` includes that day).

## External Dependencies

- `python-dateutil` (rrule expansion) with `types-python-dateutil` for mypy. `rrule()`'s `freq` argument needs `cast(Any, ...)` under mypy.
- `tzlocal` for the default host zone only.

## Constraints & Gotchas

- The `when` grammar is deliberately small; unknown expressions raise `CalendarValidationError` naming the grammar. A `start..end` range's end side is an inclusive day when given as a date.
- `update_event` rebuilds the candidate via the create path: an update that clears `rrule` re-anchors from `start_local`, drops exdates, and re-resolves the anchor zone from the current application zone. Existing recurring timed events retain their persisted `tz_name`; changing the application zone never silently rewrites stored event anchors.
- Tool tests must build fixtures relative to `service.resolve_when(...)` - the tool resolves `when` against the real clock, so hard-coded dates silently break when the month rolls over.
- In WebUI code, eslint forbids mutable `Map` in Svelte derived contexts - group with plain objects. The UI's occurrence exclusion consumes the server-provided `occurrence_start` via the additive `calendar.add_exdate` RPC (a read-modify-write of the whole `exdates` array through `calendar.update` would risk losing a concurrent tab's exclusion). Do not reconstruct the anchor client-side; the previous anchor-start reconstruction was a real bug. The edit form renders a single timed event's start in the server zone (not the raw UTC value), so a save without edits keeps the wall clock.
- The WebUI's current day, Today navigation, default event date, and agenda window use `calendar.window.system_timezone`, never the browser zone or UTC. Before the first response establishes that zone, it may make a provisional UTC-window request; when the server day differs it corrects the untouched initial anchor and reloads before rendering the projection. Agenda navigation always derives its window from the displayed anchor.
- Start the worktree server as `python -m server.main`; `python server/main.py` imports `core` through the main repo's editable install and new RPC methods come back `method_not_found`.

## References

Read these only when your task matches - not by default.

- Changing the agent-facing `calendar` Tool -> `tools/calendar.md`
