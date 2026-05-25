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
- Active jobs in `list` responses include `next_fire_at`.

## Constraints & Gotchas

- Unknown action-specific arguments return failure envelopes.
- Timezone names must be valid IANA zones where required.
- Once jobs may become `completed`; normal WebUI lists hide completed jobs.
