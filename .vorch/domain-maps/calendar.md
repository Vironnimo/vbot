# Calendar

Local-first calendar: a persisted event store with iCalendar (RFC 5545) semantics, a read-only cron projection, a WebUI tab, and exactly one agent tool.

## Overview

`core/calendar/` owns calendar events end to end: storage, recurrence expansion, single-occurrence exclusion, free-slot search, and event-relative Agent actions. `core/automation` projects cron jobs into windows on the fly (nothing persisted, never written anywhere else). The WebUI tab and the `calendar` tool are consumers of the same service; the server publishes invalidation events so every accessor refreshes when anything mutates the store.

Automation owns independent Cron schedules and shared Run admission; Calendar owns event-relative scheduling. Settings owns application timezone configuration. External calendar sync is absent: CalDAV is a future Extension (user decision 2026-08-27); the store's standard iCalendar semantics exist so that extension becomes a thin adapter. The user explicitly removed `location` and any timezone parameter from the agent surface - events anchor in the configured application timezone. Current behavior (open user decision): the Tool still reads such fields sent anyway into notes and server time (`tools/calendar.md`).

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

- Storage: `<data_dir>/calendar/events.json`, a Generation 1 document `{"format_version": 1, "events": [...]}` written atomically (contract in `settings.md`; unknown event and `rrule` fields survive saves). Invalid entries are preserved and skipped on load; if the file is unreadable the service degrades (reads return empty, mutations raise) rather than destroying data.
- Free-slot lookup loads persisted events on its own first read, including immediately after a service restart; it does not depend on an earlier event listing or mutation.
- Event-window queries honor the positive `max_per_event` caller limit independently for timed and all-day series, within the service's fixed occurrence ceiling (`test_service.py`).
- `CalendarService.actions` (`core/calendar/actions.py`) owns action definitions and execution claims in `<data_dir>/calendar/actions.json` (`{"format_version": 1, "actions": [...], "executions": {...}}`; unknown fields survive saves). Invalid entries are kept verbatim, skipped, logged and reported by `doctor config`, like Cron jobs: an invalid action never runs, and while one is kept, history rows of actions the store does not know are not pruned (the row may belong to it, and pruning would let the repaired action refire). An invalid execution row holds its occurrence - it may record a consumed claim - so that occurrence neither fires nor projects until repaired. Only an unreadable document root (not an object, another `format_version`, `actions`/`executions` missing or of the wrong type) disables action scheduling and mutation, exposes `action_error` to readers, and leaves event CRUD available. Event deletion withdraws pending admission; reconciliation removes orphan definitions. Runs already admitted keep their Session history. Execution rows are bounded: each tick drops pending rows no longer due, rows of deleted actions (action ids are never reused), and terminal rows expired more than 30 days ago whose occurrence the scan window no longer reaches; rows the scan still reaches stay as consumption records. Projections omit occurrences expired beyond that retention instead of reporting them as missed. Residual edge: an event edit that brings a pruned occurrence back into its live window with an unchanged due instant can fire it again.
- `CalendarEvent` carries exactly one start shape, enforced by validation: `start_utc` for single timed events (absolute instant), `start_local` + `tz_name` for recurring timed events (wall-clock anchor; `tz_name` is always the server zone), `start_date` for all-day events. `exdates` exist only on recurring events.
- Internal `_events.py` owns event records, input validation, persisted JSON decoding and caps (also exported by `service.py`): 2000 events, 62-day window span, 500 occurrences per event per query, 1000 exdates per event, title 200 / notes 5000 chars. `_time.py` owns timezone/instant parsing and interval arithmetic; `CalendarService` keeps catalog mutations, persistence coordination and recurrence orchestration.

## Interfaces

- `CalendarService` (`core/calendar/service.py`): `create_event`, `update_event` (roundtrips the stored event through the create shape; omitted fields keep), `delete_event`, `add_exdate`, `occurrences_in_window` (half-open UTC window), `event_span` (a timed event's anchor span with the same arithmetic as occurrence expansion - single events add real elapsed time, recurring events wall-clock time via `recurrence.resolve_local_span`; accessors must not recompute ends), `find_free_slots` (each slot is a whole free gap at least the requested duration long, not a duration-sized piece; timed events block their span, all-day events block whole local days, slots start no earlier than now on 5-minute boundaries, max 10), `parse_window` / `resolve_when`, and the live `set_timezone` seam owned by Runtime Settings application.
- RPC (`server/rpc/calendar_methods.py`): `calendar.window` returns event/Cron projections, action definitions and executions, action storage health, and `system_timezone`. Event CRUD, `calendar.add_exdate`, and action CRUD share `RESOURCE_KIND_CALENDAR` invalidations. Mutations from any accessor (including the agent tool) reach the UI through this path. Action RPCs accept a nullable Session to restore fresh Sessions; the Tool uses omission for defaults and updates.
- Agent tool `calendar` (`core/tools/calendar.py`): one flat action Tool; only `action` is universally required. A Tool-owned normalizer executes clear calls from other calendar conventions and refuses ambiguous ones with corrected calls before any change. Results are plain fields in server-local time. Its detailed contract lives in `tools/calendar.md`.
- Runtime configures, starts, stops, and awaits the action scheduler. It admits ordinary visible `RunKind.CALENDAR` Runs through `TriggerService`, with a new Session per execution unless an owned Session is selected. The Run's message is plain text built by `actions.action_message`: `Calendar action <id> is due (<when>) for "<title>" (event <id>).`, `Event time: <start> to <end> (<zone>)` (all-day: `<first> to <last day>, all day`), an optional `Event notes:` line, then `Instruction:` and the prompt on its own lines, so the Run needs no calendar knowledge to act. Identity rename retargets action references; Identity/Project removal checks them under the server reference lock.
- Cron projection: `CronService.project_occurrences(window_start, window_end)` returns `CronOccurrence` dataclasses (read-only; respects `remaining_runs` budget, capped per job). The calendar tool's `list`/`find_free` ignore cron jobs - cron is a UI layer only.

New event/action ids use `evt_`/`act_` plus 12 lowercase base32 characters, with collision checks against the loaded owning catalog before synchronous publication. Event updates keep the existing id. Coverage: `tests/core/calendar/test_service.py`, `test_actions.py`.

## Conventions

- Recurring timed events anchor wall-clock in the server timezone (09:00 stays 09:00 across DST; test-verified over the Berlin transition). Single timed events persist as UTC instants; all-day events are dates in the server zone. Clearing a recurrence re-resolves the event as a single timed event from its wall-clock start and drops any exdates (exceptions are meaningless on a single event).
- Recurrence validation rejects simultaneous `count` and `until` limits before event mutation, matching the shared RPC/Tool contract (`test_recurrence.py`, `test_service.py`).
- The application zone is injected at construction (`CalendarService(data_root, tz=...)`) from `server.timezone`, defaults to the host zone when the setting is absent, and changes live through `set_timezone`; tests pass `tz="Europe/Berlin"` explicitly for determinism. Never call `tzlocal` per operation.
- The tool layer maps `duration` to `duration_minutes`/`duration_days` by the event's kind (the start form decides: date = all-day, datetime = timed) and passes `all_day` explicitly on update so kind switches work without exposing an `all_day` parameter.
- Window bounds are inclusive days: a date bound selects its whole local day (`to` includes that day).
- `when` range parsing preserves ISO timezone designators and checks bound order in UTC after local-time resolution; a spring-gap range cannot silently produce an inverted UTC window (`test_when.py`).
- All-day overlap uses local-midnight UTC instants against the half-open query window, including windows wholly inside one day. Free-slot cursors round up again after each busy interval.
- A recurring start inside a DST gap shifts forward by that gap before wall-clock duration arithmetic; ambiguous starts use the first occurrence. COUNT retains the shifted occurrence. EXDATE accepts its original recurrence anchor or the resolved `occurrence_start` returned to accessors. Candidate windows include timezone-transition padding and filter actual UTC overlap (`test_recurrence.py`, `test_service.py`).
- Relative actions use canonical event occurrences, so event edits, EXDATEs, and all-day DST boundaries affect pending executions together. Changing an action's due instant through an event or action edit rearms its execution slot and projects the new schedule; text-only edits and timezone changes that preserve the UTC due instant retain the consumed claim. A running action retains its row until it finishes before the moved occurrence is reconciled, and admitted Runs keep their canonical Session history. Preparation actions expire at event start, actions due during an event at its end, and actions due at/after its end one hour after their due time. Claimed execution is persisted before Run admission; uncertain admissions after restart are not replayed. Every `actions.json` write goes through the `calendar-actions` `OrderedWorker` (`runtime.md`) as a deep-copied snapshot, in the order the snapshots were taken: the scheduler tick, recovery and workers await their saves off the Event Loop, and action edits (`add`/`update`/`delete`/`retarget_identity`) block on the same writer, so an edit's save lands after any in-flight scheduler save. A tick that awaited its save starts no worker when events or actions changed meanwhile (`_generation`); the woken scheduler recomputes first. A worker's pre-admission validation (Agent resolution, Session existence) runs on the Session database's pool. At most four workers run at once; each worker records claim and outcome in the stored execution row (never a recomputed copy), so an action waiting for a slot or re-dispatched after withdrawal before admission fires exactly once. Known Run claims recover their exact terminal summary from Sessions. Tests: `tests/core/calendar/test_actions.py`.

## External Dependencies

- `python-dateutil` (rrule expansion) with `types-python-dateutil` for mypy. `rrule()`'s `freq` argument needs `cast(Any, ...)` under mypy.
- `tzlocal` for the default host zone only.

## Constraints & Gotchas

Malformed persisted action fields (including invalid prompts and `when` expressions) skip that action only; an unreadable document root degrades the action store without aborting Runtime startup, keeps the original bytes untouched and leaves Calendar event access available (`test_actions.py`).

- The `when` grammar is deliberately small; unknown expressions raise `CalendarValidationError` naming the grammar. A `start..end` range's end side is an inclusive day when given as a date.
- `update_event` rebuilds the candidate via the create path while retaining its stable id: an update that clears `rrule` re-anchors from `start_local`, drops exdates, and re-resolves the anchor zone from the current application zone. Existing recurring timed events retain their persisted `tz_name` unless their start is explicitly changed; changing the application zone never silently rewrites stored event anchors.
- Tool tests must build fixtures relative to `service.resolve_when(...)` - the tool resolves `when` against the real clock, so hard-coded dates silently break when the month rolls over.
- In WebUI code, eslint forbids mutable `Map` in Svelte derived contexts - group with plain objects. The UI's occurrence exclusion consumes the server-provided `occurrence_start` via the additive `calendar.add_exdate` RPC (a read-modify-write of the whole `exdates` array through `calendar.update` would risk losing a concurrent tab's exclusion). Do not reconstruct the anchor client-side; the previous anchor-start reconstruction was a real bug. The edit form renders a single timed event's start in the server zone (not the raw UTC value), so a save without edits keeps the wall clock.
- The WebUI's current day, Today navigation, default event date, and agenda window use `calendar.window.system_timezone`, never the browser zone or UTC. Before the first response establishes that zone, it may make a provisional UTC-window request; when the server day differs it corrects the untouched initial anchor and reloads before rendering the projection. Agenda navigation always derives its window from the displayed anchor.
- Start the worktree server as `python -m server.main`; `python server/main.py` imports `core` through the main repo's editable install and new RPC methods come back `method_not_found`.

## References

Read these only when your task matches - not by default.

- Changing the agent-facing `calendar` Tool -> `tools/calendar.md`
