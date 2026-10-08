# Calendar Tool

Manages the local calendar through `CalendarService`: events and their occurrences, and free-time search. It runs nothing itself; an instruction at an event is a cron job (`tools/cron.md`, `automation.md` -> Event jobs).

## Interfaces

- Tool name: `calendar`
- Source: `core/tools/calendar.py` (registration, dispatch, windows, results, the reminder refusal) and `core/tools/_calendar_arguments.py` (call reading before validation), with `_durations.duration_minutes` and `_named_zones.named_zone` shared with the cron Tool. The reminder refusal renders its cron call with `_cron_arguments.render_call`.
- Registration: `register_calendar_tool(registry, calendar_service, *, reference_lock, cron_service=None)`. Runtime passes its `CronService`, so results name the cron jobs bound to each event; without it they name none. It registers the Tool-owned `argument_normalizer` and two unadvertised parameters only the normalizer produces: `when` (a window phrase) and `reminder` (a requested reminder the handler refuses). The schema is model-facing (`open_input_schema=True`, no `additionalProperties`), so its root properties are still the complete parameter list (`tools.md`).
- Schema (user decision 2026-10-08: the shapes Models know from Google Calendar v3 and the Google Calendar MCP servers): one flat object requiring `action` (`list`, `create`, `update`, `delete`, `find_free_time`) with `id`, `title`, `start`, `end`, `description`, `location`, `rrule` (a string), `time_min`, `time_max`, `query`, and `duration` (integer minutes, default 60). The handler requires `title` + `start` for create, `id` plus a changed field for update, `id` for delete. No time-zone parameter is advertised; `list` and `find_free_time` name the server zone (`timezone`).
- update and delete hold the Agent reference lock (`AutomationReferences.lock`, passed as `reference_lock`) like the calendar RPCs, so an event change cannot revive a cron job bound to the event between a removal's reference check and the removal (`test_changes_wait_for_the_reference_lock`).
- Display: the summary leads with the normalized `action`, followed by the first available `title`, `id`, `query`, `time_min` or `when`; persisted calls keep the Model's spelling and are normalized for the label. Calls persisted with the removed actions still render: `add_action` reads as update with a reminder, `update_action` and the other removed actions get no label (`test_display_labels_the_meant_action`). A successful `list` derives its count fact from `events`.
- Coverage: `tests/core/tools/test_calendar.py` (canonical calls, results, errors) and `tests/core/tools/test_calendar_call_tolerance.py` (accepted dialects, reminder refusals, calls with different readings), through production dispatch against a real `CalendarService` bound to a real `CronService` (`tests/core/tools/scheduling_tool_support.py`, which offers both Tools unless a test drops cron). `scripts/tool_lab/cases/automation.json` holds the probe cases; the probe shares one calendar across a file's cases, so each calendar case uses its own dates and titles. `scripts/provider_probe/scenario_automation.py` holds the canonical calls a Provider must reproduce.

## Conventions

- Times without an offset are server-local. The start form decides the event kind: a date makes an all-day event, a local date-time a timed one. `end` is exclusive (an all-day event ending on the 12th has the 13th as `end`); an all-day end on the start day means that one day. Create without `end` gives 1 hour or 1 day; update without `end` keeps the event's length (`CalendarService.update_event`), also for an occurrence.
- `id` is an event id or an occurrence id `<event id>_YYYYMMDD[THHMM]` (`calendar.md`). With an occurrence id, update changes and delete removes only that occurrence; an `rrule` there is refused with the series id. `list` with an `id` (unadvertised; the not-found hint uses it) shows that event even when none of its occurrences falls in the window.
- `rrule` is RFC 5545 RRULE text; `""` on update stops the repetition, on create it is dropped. The service validates the rule.
- Windows are half-open: `time_min` defaults to now (to the minute), `time_max` to 30 days later for list and 7 days for find_free_time, on the local wall clock. A bound is a date (local midnight) or a date-time (local unless it carries an offset). A window spans at most `MAX_WINDOW_DAYS` (62) days; a longer one is refused with the longest window from the same start. The unadvertised `when` phrase (`today`, `this week`, `next month`, `2030-01`, `a..b`; `core/calendar/when.py`) sets the whole window and is refused next to `time_min`/`time_max`.
- `query` matches case-insensitively in title, description and location of each occurrence. `duration` on find_free_time is the minimum free span; each span shown is the whole gap (`CalendarService.find_free_slots`, at most `FIND_FREE_MAX_RESULTS` 10, a note says when more follows or none was found). find_free_time ignores cron jobs.
- Results are plain fields in server-local time (`2030-01-10T15:00`, dates for all-day events), never the persisted record or UTC instants, and never carry `retryable`:
  - an event (create, update): `id`, `title`, `start`, `end` (all-day: `<end> (last day <day>)`, so a wrong exclusive end is visible), then `rrule`, `location`, `description`, `cron_jobs` when present, and for a repeating event `next` (the next 3 starts, or `none ahead`). `cron_jobs` lists `<job id> at <event time>, target <agent>` per bound job, `; `-separated, with the status appended when it is not active.
  - `list`: `events` (count), `window` (dates when it spans whole days, else local times), `timezone`, `query` when sent, then `content` with one block per event and the event fields. A repeating event's block adds `occurrences:` with one line per occurrence in the window, `<occurrence id> <start>`; a changed occurrence adds its end and the fields that differ (`title`, `location`, `description`, each cut to 40 characters). After 10 lines: `...<n> more, the last at <start>; a shorter window lists them`.
  - an occurrence update: `id`, `series`, `title`, `start`, `end`, `location`, `description`.
  - delete: `id`, `title`, `status` (`deleted`, or `deleted with all its occurrences`), and `deleted_cron_jobs` (`<id> "<name>"`, comma-separated) for the bound jobs deleted with the event; an occurrence: `id`, `title`, `start`, `status: deleted; the rest of the series stays`.
  - find_free_time: `free`, `window`, `timezone`, `note` (none found, or more follows), and `content` with one line per span `<start> to <end> (<length>)`.

## Argument reading

`_calendar_arguments.normalize_calendar_arguments` runs before schema validation and executes a call whose intent is clear; when readings differ it raises a contract error (an `invalid_arguments` failure) before any change. Every refusal starts with `calendar was not run:` and ends with the corrected call (`Send: {...}`) or one call per reading. Stand-ins in angle brackets (`<title>`, `<event id from list>`) mark values only the Agent can supply; sent back unchanged in an optional field they count as omitted, like placeholder words (`none`, `TBD`). Descriptions longer than 120 characters show as `<the description from this call>` in corrected calls.

- Accepted silently (user decision 2026-10-08): `summary`/`name`/`subject` -> title, `notes`/`details`/`body` -> description, `event_id`/`eventId` -> id, `timeMin`/`timeMax`/`from`/`to` -> time_min/time_max, `when` phrases -> window, `q`/`search` -> query, `recurrence`, an `RRULE:` prefix, a one-rule list or an old rule object (`{"freq":"weekly","by_weekday":["mo"]}`) -> rrule, Google `{date|dateTime, timeZone}` start/end, an `event`/`requestBody` wrapper, `duration`/`"1.5h"` on create or update with a start -> end (wall-clock; on an all-day start only whole days), `find_free`/`freebusy` and other action synonyms, `original_start`/`originalStartTime` with the series id -> the occurrence id. Also dropped without effect: inert Google keys (`calendarId` naming the default calendar, `sendUpdates`, `colorId`, `orderBy`, ...) and default reminders (`{"useDefault": true}` without overrides). On list, a `title` becomes `query` and `start`/`end` the window; fields an action does not use are dropped.
- A time zone (Google `timeZone`, top-level `timeZone`/`timezone`) is attached to an offset-free date-time as that zone's offset; the service converts the instant to server time. A repeating event takes the converted wall-clock time of its first occurrence for all occurrences.
- Reminders: `reminders` with overrides, `minutes_before`-style keys, `prompt`/`instruction` and the `add_reminder`/`add_action` actions become the unadvertised `reminder`, which the handler refuses: `calendar was not run: the calendar has no reminders.`, then, when cron is offered (`ToolContext.offers("cron")`), `cron runs an instruction before, at or after an event.`, the event call without the reminder (`Send:`), and the cron create call (`Then send to cron:` with the event id, `<event id from the result>` on create, the series id for an occurrence; the reminder's minutes as `start - <n>m`, else a stand-in). The event is not created in the same call (open point: one extra round trip).
- Refused with calls: attendees (offers them as a description line), another calendar, several rules, a `duration` without a start or minutes on an all-day start (`send end instead`), an `end` and `duration` that disagree, create with an `id` (offers update or create without it), delete with changes (offers delete or update), the window given twice, and a reversed window. Unknown parameters fail registry validation, which lists the parameters.
- Removed with the redesign: title lookup of an event id (the refusal now names the list call with `query`), bare clock-time ends, rule phrases (`weekly`) and top-level frequency parts, the DST refusals for repeating events in other zones, and the late-start note.

## Constraints & Gotchas

- Error codes: `invalid_arguments` (every refusal above and the service's validation errors, with the field's stand-in when the call sent it), `event_not_found` (`No event has id "<id>". {"action":"list"} shows events and their ids.`; an unknown occurrence names the list call with the series id), `calendar_storage_error` and `calendar_service_error` (`Do not repeat the same call unchanged.`).
- The Tool cannot invite attendees, keep reminders, select another calendar or hold a per-event zone; these are refused or recorded in the description rather than approximated.

## Agent-facing text

Shared reason for every row (user decision 2026-10-08): minimal, trained shapes; calendar API conventions from Google Calendar v3 / Google Calendar MCP servers. The definition is 400 estimated tokens (`python -m scripts.tool_lab definitions --all`; 370 before the redesign, which had fewer parameters and the action machinery). `description` and `location` carry no text: their names say it.

| Text | Reason |
|---|---|
| Description: `Manage the user's calendar events and find free time.` | Names the two jobs; everything else is in the parameter shapes. |
| action: `update changes only the fields you send.` | Patch semantics as in Google `events.patch`; without it Models resend every field or fear clearing them. |
| id: `Event id from list.` | Says where ids come from, so the Agent lists instead of inventing one. |
| id: `For a repeating event, an occurrence id changes or deletes only that occurrence.` | The only way to touch one occurrence (Google instances); list shows those ids. |
| id: `Required for update and delete.` | The one rule the schema cannot express per action. |
| title: `Required for create.` | Same. |
| start: `Local date-time such as 2030-01-10T15:00, or a date such as 2030-01-10 for an all-day event.` | One example per form; the form decides the event kind, so no `all_day` flag is needed. Local because no zone parameter exists. |
| start: `Required for create.` | Per-action requirement. |
| end: `Same form as start, exclusive: for an all-day event the day after the last day.` | Google/iCal convention Models know; stating it prevents the classic off-by-one, and results show the last day. |
| end: `Omit for 1 hour or 1 day.` | The default on create; update keeps the length instead (covered by "changes only the fields you send"). |
| rrule: `RFC 5545 rule such as FREQ=WEEKLY;BYDAY=MO,WE.` | The rule text every calendar API uses, one example with BYDAY. |
| rrule: `On update, "" stops repeating.` | The only way to remove a rule; a string field cannot be null. |
| time_min: `Start of the window for list and find_free_time: a date or local date-time.` | Google `timeMin` name and role. |
| time_min: `Omit to start now.` | The default. |
| time_max: `End of the window, exclusive.` | Half-open window as in Google `timeMax`. |
| time_max: `Omit for 30 days after time_min, or 7 days for find_free_time.` | The defaults per action. |
| query: `Text to find in title, description and location.` | Google `q`, with the fields it searches. |
| duration: `Minimum free time in minutes for find_free_time.` | Unit and action; the default 60 is in the schema. |
