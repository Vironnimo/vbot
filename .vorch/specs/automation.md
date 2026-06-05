# Automation

Programmatic run-triggering primitives and scheduling services in `core/automation/`.

## Overview

Automation owns kernel-level primitives for starting Runs without going through the normal WebUI send flow plus lightweight scheduling that decides when to fire those triggers. `TriggerService` starts a Run immediately when the target Session is idle and delegates busy-session queueing to `ChatLoop.queue_run(...)` / `core.runs.ChatRunManager`, which owns the shared in-memory FIFO per Session. `CronService` persists scheduled jobs under `<data_dir>/cron/jobs.json`, manages per-job asyncio tasks, and fires `TriggerService.trigger_run(...)` when jobs become due.

## Interfaces

- `TriggerService(chat_loop, chat_run_manager, runtime, trigger_chat_loop=None)` — constructed through dependency injection with the Chat loop used for command helpers, the Run manager, and optionally a separate Chat loop used for triggered Run execution. Runtime passes its streaming Chat loop as `trigger_chat_loop` so automation, cron, channel, and sub-agent batch-completion Runs emit normal streaming deltas.
- `await TriggerService.trigger_run(agent_id: str, message: str, session_id: str | None = None, *, internal: bool = False) -> Run` — creates a new Session when `session_id` is omitted, otherwise starts or queues a Run in the existing Session through the shared chat queue. When `internal=True`, the Run still starts/queues normally, but the trigger message is persisted as a kernel-internal note and embedded into the provider request as a `<system-reminder>` instead of a visible user message.
- `await TriggerService.retry_run(agent_id: str, session_id: str) -> Run` — retries the latest user turn in an existing Session through the trigger Chat loop. This does not append a new user message and does not queue behind an active Run.
- `await TriggerService.compact_session(agent_id: str, session_id: str) -> str` — performs manual compaction for command accessors and returns a user-facing reply string. It refuses active Sessions, resolves optional `summary_model` credentials the same way chat-loop compaction does, appends one `compaction_checkpoint` on success, and closes any opened adapters.
- `CronService(trigger_service, data_root)` — constructed through dependency injection with the shared `TriggerService` and the runtime data-root path.
- `CronService.create_job(...)`, `list_jobs()`, `get_job(job_id)`, `update_job(job_id, **fields)`, `delete_job(job_id)`, `enable_job(job_id)`, `disable_job(job_id)` — CRUD and status controls for persisted cron and once jobs.
- `CronService.start()` / `stop()` — sync lifecycle methods that load persisted jobs, create/cancel asyncio tasks, and are safe to call multiple times.

## Conventions

- Busy Sessions are detected through `ActiveRunError`; `TriggerService` then delegates to `ChatLoop.queue_run(...)` and awaits the queued item's start future.
- Queued triggers preserve whether they are internal or visible because that flag is stored on the shared queued Run item.
- Persisted cron-job timestamps use UTC with explicit offsets in ISO 8601 format.
- `CronService` stores jobs in `<data_dir>/cron/jobs.json` and creates the directory/file on demand.
- `cron/jobs.json` is validated through `core/settings/validation.py` before `CronService` constructs `CronJob` objects. Invalid JSON shape or schema errors raise `CronStorageError` with file/path diagnostics.
- `schedule_type` is either `cron` or `once`; job `status` is `active`, `paused`, or `completed`.
- Active cron jobs compute their next fire time with `croniter`; paused and completed jobs do not own running tasks.
- Active once jobs write a durable fire claim under `<data_dir>/cron/once-fire-claims/` before calling `trigger_run(...)`. If the completed-state save fails after the trigger succeeds, the scheduler retries that save without re-triggering. On startup, an active once job with a durable fire claim is marked `completed` without firing again, then the claim is removed after the completed state is saved.
- Trigger failures are logged by `CronService` but do not terminate active scheduler tasks. Repeating `cron` jobs continue with their next scheduled fire time. Active `once` jobs retry after a short scheduler-owned delay and are marked `completed` only after `trigger_run(...)` succeeds.
- Timezone resolution uses stdlib `zoneinfo`; the project ships `tzdata` so IANA job timezones work on platforms without a system timezone database.

## Constraints & Gotchas

- Queued busy-session work is lost on process restart because `ChatRunManager` queues are in-memory only.
- Session queue size is currently unbounded.
- Missed `once` jobs are not caught up on startup; they are logged at warn level and marked `completed`.
- Missed `cron` jobs are not replayed; the scheduler computes the next future fire.
- `once` jobs are retained after firing with `status = "completed"` and `last_fired_at` set.
- Once-job fire claims prefer at-most-once behavior across restarts: a crash after claim creation but before `trigger_run(...)` may skip that once job on restart instead of risking a duplicate run.
