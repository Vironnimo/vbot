# Subagents

Sub-agent orchestration, in-memory batch tracking, parent-child run linkage, and child result lookup.

## Overview

`core/subagents/` owns the runtime behavior behind the public `subagent` and `subagent_result` tools. The tool module is only the registration/schema boundary; `SubAgentCoordinator` performs spawn/result orchestration and owns the in-memory `SubAgentBatchTracker` for one Runtime instance.

## Data Model

- `SubAgentCoordinator`: runtime-facing service with `spawn(context, arguments)` and `result(context, arguments)` handlers.
- `SubAgentBatchTracker`: process-local tracker keyed by parent `(agent_id, session_id, run_id)`.
- Batch entries hold the child `agent_id`/`session_id`, its completion/fetched flags and captured result, and either a live `run_id` or — while still queued — a `queue_item_id`.
- Spawning also writes the durable parent→child link into the child Session's metadata: `is_subagent_session: true` plus a `subagent_parent` record (parent `agent_id`, `session_id`, `run_id`, `tool_call_id`, `tool_call_index`). The batch tracker is only the in-memory side; this metadata is what survives a restart (constants `SUBAGENT_SESSION_METADATA_FLAG` / `SUBAGENT_PARENT_METADATA_KEY`).
- Batch tracking does not persist across restarts.
- The tracker reserves a per-turn slot before async spawn work begins, so sibling tool calls cannot bypass `max_subagents_per_turn` while they are waiting on session or queue state.

## Interfaces

- `SubAgentCoordinator(runtime, trigger_service, batch_tracker=None)`
- `SubAgentCoordinator.spawn(context, arguments) -> JsonObject`
- `SubAgentCoordinator.result(context, arguments) -> JsonObject`
- `SubAgentCoordinator.batch_tracker -> SubAgentBatchTracker`
- `SUBAGENT_SESSION_STARTED_EVENT` is exported for transport layers that need to bridge the live child-Session navigation event.

## Conventions

- With `session_id`, spawning routes into an existing Session; otherwise it creates a new persisted Session for the target Agent.
- Child Runs execute through a streaming `ChatLoop`, matching normal live Runs and allowing long provider generations to make progress through stream deltas instead of waiting for one complete non-streaming response.
- An explicitly targeted existing Session that is busy enqueues a follow-up Run through `ChatRunManager`; a freshly created Session that is already busy instead fails with `session_busy` (a new Session should never be busy).
- Blocking mode waits for completion and returns the result payload.
- The `subagent` tool emits `subagent_session_started` Run events as soon as a child Session is known, then again when run/queue details are known. The event payload includes the parent tool-call id/index plus child `agent_id`, `session_id`, optional `run_id` or `queue_item_id`, and `status` so accessors can link to running child Sessions before the final tool result exists.
- Non-blocking mode returns a running descriptor when a Run has started. If the target Session is still busy and the child Run is only queued, it returns a queued descriptor containing `agent_id`, `session_id`, `queue_item_id`, and `status: "queued"` instead of waiting for the child Run to start.
- Result lookup checks live Run state first, reports queued tracked entries while no `run_id` exists, then falls back to the last non-empty assistant message in the target Session.
- Result lookup marks tracked entries fetched before waiting on a live Run to avoid a completion/fetch race.

## Constraints & Gotchas

- The caller cannot target its own active Session.
- Limits come from `runtime.storage.load_subagent_settings()` (re-normalized in-domain as a fallback): `max_subagent_depth` (default 4), `max_subagents_per_turn` (default 8), `subagent_timeout_minutes` (default 60). Full schema in `settings.md`.
- Blocking spawns wait at most `subagent_timeout_minutes` (default 60); on timeout the child Run is cancelled and the tool returns a `subagent_timeout` failure (the entry is still marked complete + fetched).
- Parent cancellation removes queued child Runs when possible and cancels already-started child Runs.
- A batch is dropped from the tracker only once *every* entry is both complete and fetched; an empty batch (no entries, no reserved slots) is dropped too.
- When all unfetched sub-agent Runs in a batch finish, the tracker sends one internal automation trigger to continue the parent Agent via a system-reminder note. The note carries each sub-agent's complete final output (untruncated) plus its run status, so the parent does not need a `subagent_result` fetch to read batch results. Runtime automation triggers use the streaming Chat loop, so this follow-up Run emits the same SSE delta timeline as a normal streamed chat turn.
- Tool descriptions instruct callers to end their turn after a non-blocking spawn and rely on the automatic completion note; `subagent_result` is only for explicit user-requested status checks before the batch finishes.
- `SubAgentCoordinator` still starts child Runs through ChatLoop internals; keep this boundary narrow until Runs exposes a cleaner child-run API.
