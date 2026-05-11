# Automation

Programmatic run-triggering primitives in `core/automation/`.

## Overview

Automation owns kernel-level primitives for starting Runs without going through the normal WebUI send flow. It does not own cron scheduling, Bash-tool callbacks, queue persistence, or priority logic. Its first service is `TriggerService`, which starts a Run immediately when the target Session is idle and queues triggers FIFO when a Session already has an active Run.

## Interfaces

- `TriggerService(chat_loop, chat_run_manager, runtime)` — constructed through dependency injection with the Chat loop and Run manager it will use.
- `await TriggerService.trigger_run(agent_id: str, message: str, session_id: str | None = None) -> Run` — creates a new Session when `session_id` is omitted, otherwise starts or queues a Run in the existing Session.

## Conventions

- Queues are keyed by `(agent_id, session_id)` and are in-memory only.
- Busy Sessions are detected through `ActiveRunError`; the service then subscribes to the active Run and drains queued triggers after terminal Run events.
- Only one subscriber task should exist per `(agent_id, session_id)` queue.

## Constraints & Gotchas

- Queued triggers are lost on process restart.
- Queue size is currently unbounded.
- `TriggerService` is a primitive for future cron jobs and background callbacks; those producers are out of scope for the service itself.
