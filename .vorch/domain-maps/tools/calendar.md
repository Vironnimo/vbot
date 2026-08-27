# Calendar Tool

Manages the local calendar through `CalendarService`: listing with expanded occurrences, single and repeating events, single-occurrence exclusion, and free-slot search.

## Interfaces

- Tool name: `calendar`
- Registration: `register_calendar_tool(registry, calendar_service)`
- Schema: one open flat object requiring only `action`, with optional siblings `when`, `id`, `title`, `start`, `duration`, `rrule`, and `notes`. The handler requires `from`-free window resolution via `when` for list/find_free, `title` + `start` for create, `id` plus at least one changed field for update, `id` for delete, and rejects every action-inapplicable or unknown field with a recommendation naming an exact valid call. The model-facing schema emits no branch keywords or `additionalProperties`; `rrule` retains the genuine `object | null` semantic because update uses explicit null to stop repetition.
- Actions: `list`, `create`, `update`, `delete`, `find_free`.
- Display: the summary leads with `action`, followed by the first available `title`, `id`, or `when` value. A successful `list` derives an exact presentation-only `occurrences` count; other actions publish no count.

## Conventions

- `when` accepts `today`, `tomorrow`, `this week`, `next week`, `this month`, `next month`, a `YYYY-MM-DD` date, a `YYYY-MM` year-month, or `start..end` (each side a date or ISO datetime; a date end side includes that whole day). `core/calendar/when.py` resolves expressions against the server zone and current time; `list` defaults to the current month, `find_free` to the next 7 days. Models never do date arithmetic.
- The start form decides the event kind: a `YYYY-MM-DD` date makes an all-day event, a datetime makes it timed. The tool never exposes `all_day`; on update it maps an explicit start change to the matching `all_day` value so kind switches work.
- `duration` is minutes for timed events and find_free slots, days for all-day events. Omitted on create it defaults to 60 minutes / 1 day; omitted on update it keeps the current length.
- `rrule` accepts `freq` (daily/weekly/monthly/yearly), `interval`, an end as `count` or `until` (never both), and `by_weekday` for weekly rules. Explicit `null` on update stops repetition; `null` on create is rejected. The tool never exposes a timezone: recurring timed events anchor wall-clock in the server zone.
- Agent-facing payloads render server-local naive ISO datetimes (or plain dates for all-day events) - never UTC instants. Occurrence payloads carry `occurrence_start` in the event's anchor form, which is exactly the EXDATE value `delete` + `start` consumes to remove one occurrence of a repeating event.
- `list` returns events that have at least one occurrence in the window (with unified `start`/`end`/`duration` fields) plus their occurrences; `find_free` returns up to five earliest free slots and ignores cron jobs.

## Constraints & Gotchas

- The registry's schema contract validates arguments before the handler runs, so enum violations surface as `ToolContractError` from dispatch, not as handler failure envelopes; the handler's unknown-action branch is a safety net only.
- Absent and explicit-null arguments are different states: an omitted `rrule` on create means a single event, an explicit `null` is invalid there, and on update omission keeps the current rule while `null` clears it.
- Unknown action-specific arguments, missing required arguments, domain validation failures, and missing events return non-retryable failure envelopes; `event_not_found` directs the Agent to `{"action":"list"}` for current ids.
- Deleting a single event with `start` fails with guidance to delete the whole event; excluding an occurrence is only valid on recurring events.