# Automation

Programmatic Run triggering and time-based scheduling in `core/automation/`.

## Overview

`TriggerService` is the kernel bridge for non-WebUI producers that need to start normal chat Runs: cron jobs, channels, sub-agent batch completion, and background tools. It does not own a second execution path; it calls the Chat domain and uses the same `ChatRunManager` active-Run guard and FIFO queue as browser sends. `CronService` owns persisted time-based jobs under `<data_dir>/cron/jobs.json`, creates one asyncio task per active job while the Runtime is started inside an event loop, and fires due jobs through `TriggerService.trigger_run(...)`. Server cron RPCs (`server/rpc/automation_methods.py`) and the `cron` tool are thin clients; scheduling behavior belongs in `core/automation/cron.py`.

## Interfaces

- `TriggerService(chat_loop, chat_run_manager, runtime, trigger_chat_loop=None)` - constructed through DI. Runtime passes the non-streaming ChatLoop for command/compaction helpers and the streaming ChatLoop as `trigger_chat_loop`, so programmatic Runs stream through the same SSE path as normal sends.
- `await trigger_run(agent_id: str, message: str | list[ContentBlock], session_id: str | None = None, *, internal: bool = False, sender: MessageSender | None = None) -> Run` - creates a Session when `session_id` is omitted, starts the Run on the trigger ChatLoop, catches `ActiveRunError`, queues through `ChatLoop.queue_run(...)`, and awaits the queued item future. `internal=True` is for string system-reminder notes only: it persists the message as a Session note instead of a visible user turn and preserves that flag when queued. `sender` is forwarded to the non-internal start/queue calls only and ends up on the persisted user message (see `chat.md`).
- `await retry_run(agent_id: str, session_id: str) -> Run` - delegates directly to the trigger ChatLoop's retry path, which reruns the latest user turn without appending a new message. Automation does not queue retry requests; busy Sessions surface `ActiveRunError` from the `ChatRunManager`.
- `await compact_session(agent_id: str, session_id: str) -> str` - command accessor helper that returns user-facing reply text. It delegates to `ChatLoop.compact_session(...)` on the non-streaming command ChatLoop; all compaction behavior (active-run refusal, settings load, summary-adapter resolution, checkpoint append, adapter close, `"Compaction failed: ..."` replies) lives in the chat domain.
- `CronService(trigger_service, data_root)` - DI service rooted at the Runtime data directory. Jobs load lazily on CRUD calls or at `start()`, are returned as clones, create the cron directory/file on demand, and are persisted with atomic JSON replace.
- `CronService.create_job(...)`, `list_jobs()`, `get_job(job_id)`, `update_job(job_id, **fields)`, `delete_job(job_id)`, `enable_job(job_id)`, `disable_job(job_id)` - persisted job CRUD/status controls. `list_jobs()` returns all jobs in stable created order, including completed once jobs.
- `CronService.start()`, `stop()`, and `aclose()` - lifecycle methods for scheduler tasks. `start()` and `stop()` are idempotent; `start()` loads jobs and starts active-job tasks, `stop()` cancels tracked tasks without awaiting their cancellation, and `aclose()` stops then awaits cancellation.

## Conventions

- `CronService` validates storage shape through `core/settings/validation.py` before constructing `CronJob` objects, then applies semantic validation in `core/automation/cron.py`. Invalid storage raises `CronStorageError`; bad job data or unsupported update fields raise `CronJobValidationError`.
- `schedule_type` is `cron` or `once`. Cron jobs require `cron_expression` and clear `run_at`; once jobs require `run_at` and clear `cron_expression`.
- `status` is `active`, `paused`, `completed`, or `failed`. Paused, completed, and failed jobs do not own scheduler tasks. `completed` cannot be re-enabled or paused; `failed` is a system-assigned terminal state for a once job that gave up after repeated fire failures (never set by callers) and *can* be re-enabled to retry once the cause is fixed.
- `created_at` and `last_fired_at` are UTC timestamps with explicit offsets. `run_at` may be timezone-aware or naive; naive once-job times are interpreted in the job timezone, then the local timezone, then UTC as a final fallback.
- Cron expressions are evaluated with `croniter` in the job timezone when present, otherwise in the host local timezone. `UTC` is accepted without consulting `zoneinfo`; other IANA names use `zoneinfo`, with `tzdata` available for platforms without a system database.
- Changing `schedule_type`, `cron_expression`, `run_at`, `timezone`, or `status` restarts an active scheduler task. Prompt, agent, and session updates do not restart a sleeper; the task reloads the latest job before firing.
- `CronService` validates strings and schedules but does not verify that `agent_id` exists. Server cron RPCs validate agent references before create/update; other producers must do that themselves if they need immediate feedback.
- Active once jobs write a durable fire claim under `<data_dir>/cron/once-fire-claims/` before calling `trigger_run(...)`. If trigger fails, the claim is removed and the job retries with bounded exponential backoff, and is abandoned (marked `failed`) once `_ONCE_MAX_FIRE_ATTEMPTS` is reached so a permanently failing once job stops looping; if completed-state save fails after a successful trigger, the scheduler retries that save without firing again.

## Constraints & Gotchas

- Runtime startup without a running asyncio loop wires `CronService` but does not start scheduler tasks; `Runtime._start_cron_service()` only calls `start()` when `asyncio.get_running_loop()` succeeds.
- Busy-session queueing is in-memory and unbounded, so queued triggered work is lost on process restart.
- Missed once jobs are not caught up on startup; they are logged at warn level and marked completed without firing. Missed cron jobs are not replayed; the next future fire is computed from current time.
- Once jobs are retained after firing with `status = "completed"` and `last_fired_at` set.
- Once-job fire claims intentionally prefer at-most-once behavior across restarts: a crash after claim creation but before `trigger_run(...)` may skip that once job on restart instead of risking duplicate execution.
- Trigger failures are logged by `CronService` and do not kill scheduler tasks. Cron jobs continue to their next computed fire; once jobs retry a failed fire (claim write or trigger) with bounded exponential backoff (`_ONCE_RETRY_DELAY_SECONDS` base, `_ONCE_RETRY_BACKOFF_FACTOR`, capped at `_ONCE_RETRY_MAX_DELAY_SECONDS`) and are abandoned (marked `failed`, `last_fired_at` left unset) after `_ONCE_MAX_FIRE_ATTEMPTS` failed attempts. The completed-state save after a successful trigger still retries until it persists.
