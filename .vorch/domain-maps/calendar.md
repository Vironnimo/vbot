# Calendar

Local-first calendar: a persisted event store with iCalendar (RFC 5545) semantics, read by Cron jobs bound to events, shown in a WebUI tab and changed through exactly one agent tool.

## Overview

`core/calendar/` owns calendar events end to end: storage, RRULE expansion, changed and removed occurrences of repeating events, and free-slot search. It runs nothing itself: an Agent instruction bound to an event is a Cron job of schedule type `event` (`automation.md` -> Event jobs). The former Calendar actions (`core/calendar/actions.py`, `calendar/actions.json`, `calendar.*_action` RPCs, the Tool's `add_action`/`update_action`/`delete_action`) were removed for them (user decision 2026-10-08); stored Runs of kind `calendar` and Skill-history references of kind `calendar` stay readable as history. `core/automation` projects Cron jobs into windows on the fly (nothing persisted). The WebUI tab and the `calendar` tool consume the same service; the server publishes invalidation events so every accessor refreshes when anything mutates the store.

Automation owns all scheduling and Run admission; Settings owns application timezone configuration. External calendar sync is absent: CalDAV is a future Extension (user decision 2026-08-27); the store's standard iCalendar semantics exist so that extension becomes a thin adapter. `location` is a real free-text event field (user decision 2026-10-08). No time-zone parameter is advertised: input times without an offset are server-local, and events keep the server zone of their creation.

Open (in progress on this branch): the `calendar` Tool still has its earlier parameters (`notes` for the description, one occurrence removed by `delete` with `start`) mapped onto the new event fields (`tools/calendar.md`), and the WebUI still calls the removed `calendar.add_exdate` and `calendar.*_action` RPCs.

## Terms

Core terms (Run, Session, Tool) live in `.vorch/GLOSSARY.md`.

### Occurrence
One instance of an event, produced on the fly by expansion with its changes applied and never persisted (`EventOccurrence`). A single event has exactly one, whose id is the event id. `start`/`end` are in the event's own form; `start_utc`/`end_utc` are the instants.

### Occurrence key, occurrence id
The key of an occurrence of a repeating event is its original start in the event's form: a date, or a local date-time to the second. Removed and changed occurrences are stored under it, and moving a series moves the keys along. The occurrence id is the event id plus `_YYYYMMDD` (all-day) or `_YYYYMMDDTHHMM` (`SS` appended only when the seconds are not zero), for example `evt_abc123def456_20300110T0900` (`occurrence_id`, `parse_occurrence_id`). RPC `calendar.update`/`calendar.delete` and `CalendarService.get_occurrence`/`update_occurrence`/`delete_occurrence` take it.

### Changed occurrence (override)
Per-occurrence changes of a repeating event, like an iCalendar RECURRENCE-ID or a Google instance: title, description, location, and start with end. A missing field keeps the series' value, a null description or location clears it, and a change back to the series' value is dropped. A changed occurrence keeps the event's kind (timed or all-day).

### Removed occurrence (EXDATE)
RFC 5545 exception that removes one occurrence from a repeating event while keeping the series (`delete_occurrence`); its change goes with it.

### when expression
The agent-facing window grammar (`today`, `this week`, `next month`, a date, a year-month, `start..end`) parsed by `core/calendar/when.py` against the server zone and current time, so models never do date arithmetic. Not an ISO window; RPC `calendar.window` takes explicit `from`/`to` bounds.

## Data Model

- Storage: `<data_dir>/calendar/events.json`, a Generation 1 document `{"format_version": 2, "events": [...]}` written atomically (contract in `settings.md`; unknown event fields survive saves). A version 1 document (events of earlier vBot versions) is ignored with a load WARNING and a `doctor config` warning and replaced by the next change; there is no converter (user decision 2026-10-08: no relevant old data). Invalid entries are preserved and skipped on load and reported by `doctor config` at their item path, isolating only their event; if the file is unreadable, or cannot even be checked, the service degrades (reads return empty, mutations raise) rather than destroying data; only a missing file is seeded empty (`test_service.py`).
- `CalendarEvent` (`_events.py`): `title`, `description`, `location`, `start`, `end`, `tz_name`, `rrule`, `exdates`, `overrides`, `created_at`, `updated_at`. A timed event keeps `start`/`end` as naive local wall-clock times in `tz_name`, the server zone at its creation (a single timed event too, so a time in a repeated hour of the DST fall-back loses which of the two it was). An all-day event has `tz_name: null` and keeps dates with an exclusive `end`; its days start at local midnight in the current server zone. An omitted end is one hour or one day after the start; an all-day end equal to the start means that one day. `rrule` is one RRULE text without the `RRULE:` prefix, stored upper-case with `FREQ` first (`recurrence.normalize_rrule`); `exdates` and `overrides` exist only on a repeating event, keyed by occurrence key.
- Limits (`_events.py`, `recurrence.py`): 2000 events, 62-day query window, 500 occurrences per event per query, 1000 removed and 1000 changed occurrences per event, title 200 / description 5000 / location 500 chars, timed events up to 30 days and all-day events up to 365 days long, start years 1000-9000. RRULE: `FREQ` DAILY, WEEKLY, MONTHLY or YEARLY with every RFC 5545 part except BYSECOND (BYHOUR/BYMINUTE refused for all-day events, at most 24 starts a day), INTERVAL up to 1000, COUNT up to 10000, COUNT or UNTIL but not both, at most 500 chars; a rule without any occurrence is refused (`test_recurrence.py`).
- Internal `_events.py` owns records, occurrence ids, JSON validation and caps; `_expansion.py` owns pure expansion (window, lazy iteration, one occurrence by key); `recurrence.py` owns RRULE parsing and DST resolution; `_time.py` owns timezone/instant helpers. `CalendarService` keeps catalog mutations, persistence and the binding to event jobs.

## Interfaces

- `CalendarService` (`core/calendar/service.py`): `create_event`, `get_event`, `list_events`, `update_event` (async; omitted fields keep, a new start alone keeps the length, an empty or null `rrule` stops the repetition, a moved series moves its removed and changed occurrences and drops those the new rule no longer produces, a change between timed and all-day drops them), `delete_event` (async; returns the `BoundJob`s deleted with it), `get_occurrence`, `update_occurrence` and `delete_occurrence` (async, by occurrence id), `occurrences_in_window` (half-open UTC window; the positive `max_per_event` limit applies to each event, `test_occurrence_limit_applies_to_each_event`) and `event_occurrences`, `iter_occurrences(event, after)` (lazy, the occurrences ending after `after` in start order, moved ones merged in), `next_start`, `find_free_slots` (each slot is a whole free gap at least the requested duration long, not a duration-sized piece; timed events block their span, all-day events whole local days; slots start no earlier than now on 5-minute boundaries, max 10; it loads persisted events on its own first read, also right after a restart), `parse_window` / `resolve_when`, `add_changed_callback`, and the live `set_timezone` seam owned by Runtime Settings application (it announces a change, which wakes event jobs).
- Event jobs: Runtime binds the Cron service with `bind_event_jobs` (`EventJobs` protocol in `core/calendar/event_jobs.py`). Before storing a changed event or occurrence, `_store_change` awaits `check_event_change(before, after)`, which may refuse with `EventJobTargetMissingError` (a `CalendarValidationError`) and change nothing; after deleting an event, `delete_event` awaits `event_deleted(event_id, actor=)`. When that fails, the event stays deleted and the error is raised; the jobs then wait without an event and no longer count as references. The calendar never imports automation; automation reads events through its own narrow protocol (`automation.md` -> Event jobs). Event and occurrence changes and deletions run one at a time (`_edits` lock); creation is synchronous.
- RPC (`server/rpc/calendar_methods.py`): `calendar.window` returns `occurrences`, `events`, the Cron projection `cron` (`job_id`, `name`, `fire_at`, `schedule_type`, and for an event job `event_id` and `occurrence_id`) and `system_timezone`. `calendar.create` and `calendar.update` take `title`, `start`, `end`, `description`, `location`, `rrule` (null or "" clears a text field); `calendar.update` and `calendar.delete` take an event id or an occurrence id (`rrule` is refused with an occurrence id). Deleting an event returns `{id, deleted, cron_jobs: [{id, name}]}`, deleting an occurrence `{id, deleted, event}`. `calendar.update` and occurrence deletion hold the Agent reference lock, because the change may revive an event job (`automation.md` -> References). All mutations share `RESOURCE_KIND_CALENDAR` invalidations, so changes from any accessor (including the agent tool) reach the UI.
- Agent tool `calendar` (`core/tools/calendar.py`): one flat action Tool; only `action` is universally required. A Tool-owned normalizer executes clear calls from other calendar conventions and refuses ambiguous ones with corrected calls before any change. Results are plain fields in server-local time. `update` and `delete` hold the Agent reference lock (`register_calendar_tool(..., reference_lock=)`). Its detailed contract lives in `tools/calendar.md`.
- Cron projection: `CronService.project_occurrences(window_start, window_end)` returns `CronOccurrence` dataclasses (read-only; respects `remaining_runs`, capped per job; an event job's rows name the occurrence). The calendar tool's `list`/`find_free` ignore Cron jobs.

Logging (policy: `logging.md`): event and occurrence mutations log one INFO line at the service with changed field names (or the occurrence key) and `actor=` (`rpc`, `tool`, `internal` by default) - never titles, descriptions or locations (`test_service.py`).

New event ids use `evt_` plus 12 lowercase base32 characters, with collision checks against the loaded catalog before synchronous publication. Event updates keep the existing id. Coverage: `tests/core/calendar/test_service.py`, `test_recurrence.py`, `test_when.py`; event jobs in `tests/core/automation/test_cron_events.py`.

## Conventions

- Repeating timed events anchor wall-clock in their zone (09:00 stays 09:00 across DST): dateutil expands the rule on naive local times and the zone is attached afterwards. A start inside a DST gap shifts forward by that gap and keeps its wall-clock length; ambiguous starts use the first occurrence (`recurrence.resolve_local_span`, `test_recurrence.py`). A changed start re-normalizes the rule, whose meaning depends on the start (weekday, UNTIL, BYHOUR).
- The application zone is injected at construction (`CalendarService(data_root, tz=...)`) from `server.timezone`, defaults to the host zone when the setting is absent, and changes live through `set_timezone`; tests pass `tz="Europe/Berlin"` explicitly for determinism. Never call `tzlocal` per operation. A time with an offset names its instant and is converted into the event's zone (the server zone for a new event).
- Window bounds are inclusive days: a date bound selects its whole local day (`to` includes that day).
- `when` range parsing preserves ISO timezone designators and checks bound order in UTC after local-time resolution; a spring-gap range cannot silently produce an inverted UTC window (`test_when.py`).
- All-day overlap uses local-midnight UTC instants against the half-open query window, including windows wholly inside one day. Free-slot cursors round up again after each busy interval.

## External Dependencies

- `python-dateutil` (rrule expansion) with `types-python-dateutil` for mypy.
- `tzlocal` for the default host zone only.

## Constraints & Gotchas

- The `when` grammar is deliberately small; unknown expressions raise `CalendarValidationError` naming the grammar. A `start..end` range's end side is an inclusive day when given as a date.
- A repeating event's moved occurrence can start far from its original start: expansion merges moved occurrences into the rule's stream (`_expansion.py`), so callers must use the service's expansion instead of the rule.
- Tool tests must build fixtures relative to `service.resolve_when(...)` - the tool resolves `when` against the real clock, so hard-coded dates silently break when the month rolls over.
- In WebUI code, eslint forbids mutable `Map` in Svelte derived contexts - group with plain objects. The edit form renders a single timed event's start in the server zone (not the raw UTC value), so a save without edits keeps the wall clock.
- The WebUI's current day, Today navigation, default event date, and agenda window use `calendar.window.system_timezone`, never the browser zone or UTC. The view's place (`webui/app-shell.md` -> Navigation) is `[mode, 'YYYY-MM-DD']` (mode `month`, `week`, `day` or `agenda`); Prev, Next, Today and each mode tab are history steps, layer toggles are not. Before the first response establishes the zone, the view may make a provisional UTC-window request; an empty place is corrected to today in the server zone only once that zone is known, and an explicit day in the place is never moved. Agenda navigation always derives its window from the displayed anchor.
- Start the worktree server as `python -m server.main`; `python server/main.py` imports `core` through the main repo's editable install and new RPC methods come back `method_not_found`.

## References

Read these only when your task matches - not by default.

- Changing the agent-facing `calendar` Tool -> `tools/calendar.md`
