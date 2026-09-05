# Calendar Tool

Manages the local calendar through `CalendarService`: listing with expanded occurrences, single and repeating events, single-occurrence exclusion, free-slot search, and event-relative Agent actions.

## Interfaces

- Tool name: `calendar`
- Registration: `register_calendar_tool(registry, calendar_service)`
- Schema: one open flat object requiring only `action`. Event operations retain their existing fields; Agent actions add only `prompt`, `target`, and `session`, reusing `id` and `when`. The handler requires `title` + `start` for create, `id` plus changed fields for update, and `id` for delete. It rejects every action-inapplicable or unknown field with a recommendation naming an exact valid call. The model-facing schema emits no branch keywords or `additionalProperties`; `rrule` retains the genuine `object | null` semantic because update uses explicit null to stop repetition.
- Actions: `list`, `create`, `update`, `delete`, `find_free`, `add_action`, `update_action`, `delete_action`.
- Display: the summary leads with `action`, followed by the first available `title`, `id`, or `when` value. A successful `list` derives an exact presentation-only `occurrences` count; other actions publish no count.

## Conventions

- `when` accepts `today`, `tomorrow`, `this week`, `next week`, `this month`, `next month`, a `YYYY-MM-DD` date, a `YYYY-MM` year-month, or `start..end` (each side a date or ISO datetime; a date end side includes that whole day). `core/calendar/when.py` resolves expressions against the server zone and current time; `list` defaults to the current month, `find_free` to the next 7 days. Models never do date arithmetic.
- The start form decides the event kind: a `YYYY-MM-DD` date makes an all-day event, a datetime makes it timed. The tool never exposes `all_day`; on update it maps an explicit start change to the matching `all_day` value so kind switches work.
- `duration` is minutes for timed events and find_free slots, days for all-day events. Omitted on create it defaults to 60 minutes / 1 day; omitted on update it keeps the current length.
- `rrule` accepts `freq` (daily/weekly/monthly/yearly), `interval`, an end as `count` or `until` (never both), and `by_weekday` for weekly rules. Explicit `null` on update stops repetition; `null` on create is rejected. The tool never exposes a timezone: recurring timed events anchor wall-clock in the server zone.
- Agent-facing payloads render server-local naive ISO datetimes (or plain dates for all-day events) - never UTC instants. Occurrence payloads carry `occurrence_start` in the event's anchor form, which is exactly the EXDATE value `delete` + `start` consumes to remove one occurrence of a repeating event.
- `list` returns events that have at least one occurrence in the window plus their occurrences; `find_free` returns up to five earliest free slots and ignores cron jobs. Timed event payloads carry `start`/`end` with no `duration` (it is end minus start); all-day payloads carry `start` and `duration` in days.
- `add_action` needs event `id`, `when`, and a self-contained `prompt`. Its `when` is `start` or `end`, optionally followed by `+` or `-` and an integer duration in `m`, `h`, or `d` (for example `start - 1h`). Omitted `target` uses the calling Agent and Project; explicit targets use `agent` or `agent@project`. Omitted `session` creates a fresh Session on each execution; an explicit Session must belong to that target.
- `update_action` and `delete_action` take the action id returned by `list` or `add_action`. Update omissions preserve the saved values. `list` includes action definitions, occurrence execution status, concrete due/expiry timestamps, and admitted Run/Session ids. Calendar owns expiry and recovery defaults (`calendar.md`); they are not Tool parameters. The action instruction controls any desired notification behavior.

## Constraints & Gotchas

- The registry's schema contract validates arguments before the handler runs, so enum violations surface as `ToolContractError` from dispatch, not as handler failure envelopes; the handler's unknown-action branch is a safety net only.
- Absent and explicit-null arguments are different states: an omitted `rrule` on create means a single event, an explicit `null` is invalid there, and on update omission keeps the current rule while `null` clears it.
- Unknown action-specific arguments, missing required arguments, domain validation failures, and missing events return non-retryable failure envelopes; `event_not_found` directs the Agent to `{"action":"list"}` for current ids.
- Deleting a single event with `start` fails with guidance to delete the whole event; excluding an occurrence is only valid on recurring events.
