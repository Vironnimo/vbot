# Cron Tool

Manages persisted time-based automation jobs through `CronService`.

## Interfaces

- Tool name: `cron`
- Registration: `register_cron_tool(registry, cron_service)`
- Schema: one flat discriminated union with a closed branch per required `action`. Each branch advertises only its valid fields and structurally requires them: `create` requires `prompt` + `schedule`; `update` requires `id` plus at least one changed field; delete/enable/disable require `id`; list contains only `action`. The handler retains the same validation as defense in depth.
- Actions: `create`, `list`, `update`, `delete`, `enable`, and `disable`. `create` requires `prompt` + `schedule`; `update` requires `id` + at least one changed field; delete/enable/disable require `id`; list needs only `action`.
- Display: the summary leads with `action`, followed by available `name`, `id`, target, and schedule values.

## Conventions

- `create` defaults an omitted `target` to `ToolContext.agent_id` + `ToolContext.project_id`; an explicit target is parsed as `agent` or `agent@project`. Cross-Agent and cross-Project administration is intentional: `list` returns all jobs and every mutating operation may address a job owned by another target. CronService owns target existence validation.
- `schedule` has four accepted forms: an ISO 8601 timestamp, `in <duration>`, `every <duration>`, or exactly five cron fields. Durations are positive whole numbers followed by `m`, `h`, or `d`; bare durations, fuzzy dates, seconds, and six-field cron are rejected. Offset-free ISO timestamps and cron use the server timezone.
- `repeat` controls the number of future fires, including the next one. On create, an omitted value makes a recurring job unlimited and a Once job implicitly uses one. On update, omission preserves the current count even when `schedule` changes, a positive integer replaces it, and explicit `null` makes a recurring job unlimited. Once rejects `null` and values other than one; changing an incompatible recurring job to Once without `repeat` fails and directs the caller to send `repeat: 1`.
- `name` is optional on create. CronService derives it once from the first useful prompt line after collapsing whitespace and stripping a Markdown prefix, caps it at 80 characters, and falls back to `Scheduled Run`; later prompt updates never rename the job. Explicit names remain non-unique.
- The Tool does not expose `session_id`, `timezone`, or direct `status`. Every created job starts a fresh Session on each fire, new jobs are active immediately, and state changes use `enable` and `disable`. Fixed Sessions and direct status updates remain service/RPC/CLI/WebUI capabilities.
- Tool list payloads use `CronService.next_fire_at()` and include canonical `schedule`, `remaining_runs`, `name`, `system_timezone`, formatted target, terminal history, and persisted execution-health fields. `next_fire_at` is available only for active jobs with at least one remaining fire.

## Constraints & Gotchas

- Unknown action-specific arguments, missing required arguments, domain validation failures, and missing jobs return non-retryable failure envelopes whose message includes an exact valid call or directs the Agent to `{"action":"list"}` for current ids.
- The canonical registry rejects the retired nested `request.operation` envelope, operation-key envelopes, stringified operation objects, the old `agent_id` spelling, and removed `timezone`, `session_id`, or `status` arguments before the handler runs.
- The Tool never accepts a timezone. Cron expressions and offset-free Once values use the server's IANA system timezone, including future DST rules; an explicit Once offset remains an absolute instant.
- A repeat is consumed when `TriggerService` admits or queues the Run, before its terminal result. A failed admitted Run therefore counts; a trigger failure before admission does not. Missed Once jobs do not catch up after restart; list reports them as `missed`. Repeated recurring Run failures eventually stop a job as `failed`; enable retries it after resetting the consecutive-failure streak.
