# Cron Tool

Manages persisted time-based automation jobs through `CronService`.

## Interfaces

- Tool name: `cron`
- Registration: `register_cron_tool(registry, cron_service)`
- Schema: required `action`; action-specific job fields for `create`, `list`, `update`, `delete`, `enable`, and `disable`.
- Display: summary fields `action`, `id`, `agent_id`, and `schedule_type`.

## Conventions

- `create` defaults an omitted `agent_id` to `ToolContext.agent_id` + `ToolContext.project_id`; an explicit target is parsed as `agent` or `agent@project`. This is the Tool's only address parse: CronService owns existence and fixed-Session validation.
- `create` and `update` validate schedule fields through `CronService`. Cron uses exactly five fields with a minimum one-minute cadence; Once accepts ISO 8601 and interprets offset-free values in `timezone`.
- Omitting `session_id` creates a fresh Session for every fire. A supplied id must be an existing Session owned by the target.
- Tool list payloads use `CronService.next_fire_at()`/`effective_timezone_name()` and include `system_timezone`, the formatted target, terminal history, and persisted execution-health fields. `next_fire_at` is available for active Cron and Once jobs and is `null` for non-active jobs.

## Constraints & Gotchas

- Unknown action-specific arguments return failure envelopes (the allowed argument set is per-action).
- Timezone names must be valid IANA zones where required. An omitted zone uses the server's IANA system timezone, including future DST rules.
- Missed Once jobs do not catch up after restart; list reports them as `missed`. Repeated recurring Run failures eventually stop a job as `failed`; enable retries it after resetting the consecutive-failure streak.
