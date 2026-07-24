# Cron Tool

Manages persisted time-based automation jobs through `CronService`.

## Interfaces

- Tool name: `cron`
- Registration: `register_cron_tool(registry, cron_service)`
- Schema: one root operation envelope containing exactly one of `create`, `list`, `update`, `delete`, `enable`, or `disable`; fields inside that operation object are flat and operation-specific.
- Display: the operation-specific summary uses the job `id`, target, and schedule type when available.

## Conventions

- `create` defaults an omitted `target` to `ToolContext.agent_id` + `ToolContext.project_id`; an explicit target is parsed as `agent` or `agent@project`. Cross-Agent and cross-Project administration is intentional: `list` returns all jobs and every mutating operation may address a job owned by another target. CronService owns target existence validation.
- `create` and `update` validate schedule fields through `CronService`. Cron uses exactly five fields with a minimum one-minute cadence; Once accepts ISO 8601 and interprets offset-free values in the server timezone.
- The Agent-facing Tool does not expose `session_id`, `timezone`, or `status`. Every created job starts a fresh Session on each fire, new jobs are active immediately, and state changes use the dedicated `enable` and `disable` operations. Fixed Sessions and direct status updates remain service/RPC/CLI/WebUI capabilities.
- Tool list payloads use `CronService.next_fire_at()` and include `system_timezone`, the formatted target, terminal history, and persisted execution-health fields. `next_fire_at` is available for active Cron and Once jobs and is `null` for non-active jobs.

## Constraints & Gotchas

- Unknown operation-specific arguments, multiple operations, missing required arguments, domain validation failures, and missing jobs return non-retryable failure envelopes.
- The handler accepts the legacy flat `{action, ...}` structural shape for compatibility, mapping `agent_id` to `target`, but still rejects the removed `timezone`, `session_id`, and `status` arguments.
- The Tool never accepts a timezone. Cron expressions and offset-free Once values use the server's IANA system timezone, including future DST rules; an explicit Once offset remains an absolute instant.
- Missed Once jobs do not catch up after restart; list reports them as `missed`. Repeated recurring Run failures eventually stop a job as `failed`; enable retries it after resetting the consecutive-failure streak.
