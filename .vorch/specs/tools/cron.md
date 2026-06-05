# Cron Tool

Manages persisted time-based automation jobs through `CronService`.

## Interfaces

- Tool name: `cron`
- Registration: `register_cron_tool(registry, cron_service)`
- Schema: required `action`; action-specific job fields for `create`, `list`, `update`, `delete`, `enable`, and `disable`.
- Display: summary fields `action`, `id`, `agent_id`, and `schedule_type`.

## Conventions

- `create` and `update` validate schedule fields through `CronService`.
- Cron expressions are validated with `croniter`.
- `next_fire_at` is computed only for active `cron` jobs; it is `null` for `once`, paused, or completed jobs.

## Constraints & Gotchas

- Unknown action-specific arguments return failure envelopes (the allowed argument set is per-action).
- Timezone names must be valid IANA zones where required.
- The `list` action returns all jobs, including `completed` once-jobs — unlike the WebUI, which hides completed jobs from its normal list.
