# Runs

Run lifecycle, cancellation, replayable timeline events, and in-memory busy-session queue coordination.

## Overview

`core/runs/` owns the provider-agnostic execution envelope around one active Session turn. It does not own provider/tool execution, ChatMessage construction, Session persistence, server transport, or WebUI state; those domains consume Run state through `core.runs`. `ChatLoop` builds the `RunExecutor` and calls `ChatRunManager.start(...)` or `enqueue(...)`; the manager creates the `Run`, runs the executor in a background task, publishes lifecycle events, and drains queued work.

## Data Model

- `RunStatus` — `running`, `completed`, `failed`, or `cancelled`.
- `RunEvent` — replayable timeline event with Run-local `sequence`, ids, `type`, JSON `payload`, and UTC ISO `timestamp`. It also carries `project_id` (the emitting Run's anchor, `None` for an identity run): `agent_id` stays bare, so a consumer that keys by the outside `agent@projekt` address rebuilds it from this field. Mirrored into `to_dict()` and the `/ws` bridge payload.
- `Run` — active execution state: bounded replay buffer, bounded live subscriber queues, cancellation callbacks, terminal status/result/error, and the executor task used for best-effort cancellation.
- `QueuedRunItem` — pending request with display preview, executor, `internal` flag, created timestamp, and `future`; the future resolves to the started `Run` or is cancelled if the queued item is removed before start.
- `ChatRunManager` — in-memory owner of active Runs, completed-Run lookup retention, per-session FIFO queues, cancellation, and Run-start callbacks.
- `RunExecutor` — async callable receiving the `Run` object and returning the final result. The manager translates returned results, raised errors, and cancellation into terminal Run state.
- `RunError`, `ActiveRunError`, `RunNotFoundError`, and `RunCancelledError` are expected domain errors for caller/RPC mapping.

## Event Contract

Most stable event constants live in `core.runs`: lifecycle (`run_started`, `run_completed`, `run_failed`, `run_cancelled`), output (`user_message_persisted`, `reasoning`, `tool_call_started`, `tool_call_result`, `assistant_output`, `error_message_persisted`, `model_fallback_activated`, `compaction_completed`), and SSE-only deltas (`assistant_output_delta`, `reasoning_delta`, `tool_call_delta`, `tool_call_stdout`, `tool_call_stderr`). `subagent_session_started` is emitted on a Run timeline too, but its constant belongs to `core.subagents` because the payload is owned by the sub-agent domain.

Every emitted event increments the Run-local `sequence`, including transient deltas. Sequences are monotonic for that Run and are never reused when old events fall out of the retained replay window. `Run.subscribe(after_sequence=...)` replays retained events with larger sequence numbers, then follows live events until a terminal event; late subscribers can only replay the retained window.

Run event payload ownership stays with the domain that emits the event. `core/chat/` owns ChatMessage, tool-call, fallback, compaction, and error-message payloads; `core/subagents/` owns `subagent_session_started`; `server/` only maps Run events to SSE/WebSocket/RPC payloads and strips opaque provider metadata.

## Interfaces

- `Run.emit(event_type, payload=None) -> RunEvent | None` appends and publishes an event unless the Run is terminal or cancellation suppression drops a non-terminal event. The `run_started` payload is `{"status": "running"}` with an optional `queue_item_id` when the run was started from a queued item.
- `Run.subscribe(after_sequence=0)` streams retained and future events until terminal state; lagging live subscribers are evicted instead of building unbounded queues.
- `Run.wait()` waits for terminal state, returns the executor result, re-raises failures, and raises `RunCancelledError` for cancelled Runs.
- `Run.request_cancel(reason=None)` marks cancellation requested, stores the optional `reason` on `Run.cancel_reason` for inclusion in the `run_cancelled` terminal payload, schedules registered sync/async cancel callbacks, and cancels the background executor task if one exists.
- `Run.add_cancel_callback(callback)` registers cleanup for active provider/tool/host work; callbacks registered after cancellation is already requested are scheduled immediately.
- `Run.register_tool_cancel(tool_call_id, callback)` registers a per-tool-call cancel callback without touching the run's overall cancellation state.
- `Run.cancel_tool_call(tool_call_id) -> bool` invokes the registered per-tool-call callback and marks the call user-cancelled; returns `False` for unknown or already-cancelled ids. It does NOT set `cancel_requested` and does NOT cancel the executor task — it is strictly separate from `request_cancel`.
- `Run.tool_call_cancelled(tool_call_id) -> bool` reports whether a specific tool call was user-cancelled via `cancel_tool_call`.
- `Run.clear_tool_cancel(tool_call_id)` removes the per-tool-call cancel registry entry.
- `Run.raise_if_cancelled()` lets executors stop between provider/tool steps once cancellation was requested.
- `ChatRunManager.start(agent_id, session_id, executor, *, project_id=None)` starts immediately or raises `ActiveRunError` when that Session already has a running Run. `enqueue(...)` starts immediately when idle and resolves the item future at once; otherwise it appends a FIFO `QueuedRunItem` for that session key. The optional `project_id` is stored on the created `Run` (and carried on `QueuedRunItem` until drain) — it does **not** enter the dedup key.
- **The dedup key is `(agent_id, session_id)`** (`SessionKey`), not the project triple. Session ids are globally unique UUIDs, so `(agent_id, session_id)` is already unique without the project; putting `project_id` in the key would be both redundant and fragile (any lookup caller that didn't know the project — e.g. `chat_methods` `active_run(agent_id, session_id)` — would miss a project-scoped run and let a second run start on the same session). So the lookup methods (`active_run`, `cancel_by_session`, `list_queued`, `remove_queued`, `update_queued`) take no `project_id`. The project rides on `Run.project_id` purely so session **I/O** (the chat loop opening the transcript under the project anchor) can find the right path — never for keying.
- `ChatRunManager.list_queued(...)`, `remove_queued(...)`, and `update_queued(...)` are raw queue controls. They include internal items; public RPC filtering belongs in `server/rpc/chat_methods.py`.
- `ChatRunManager.get(run_id)`, `active_run(...)`, `cancel(run_id)`, and `cancel_by_session(...)` are the lookup/cancellation surface used by server RPCs, slash commands, channels, tools, and sub-agent cleanup.
- `ChatRunManager.has_activity_for_agent(agent_id)` reports whether an agent owns any active or queued work.
- `ChatRunManager.has_activity_for_session(agent_id, session_id)` reports whether one Session owns an active or queued Run, keyed on the exact `(agent_id, session_id)` pair both the active-run and queue maps use. `session.delete` calls it to refuse removing a Session with work in flight.
- `ChatRunManager.active_runs() -> list[Run]` returns a snapshot (fresh list) of all entries in `_active_by_session` whose `status == RunStatus.RUNNING`. Public accessor mirroring `active_run(...)`; used by the `/ws` handshake to include active runs in the `connection_ready` snapshot.

## Cross-Domain Contracts

- `core/chat/` owns provider calls, tool execution, message persistence, retry/fallback behavior, and which Run events to emit. New chat execution paths should call the manager instead of constructing `Run` directly.
- `core/sessions/` owns durable history. Run timelines are process-local replay buffers and are not a substitute for JSONL Session history.
- `server/` exposes `Run.events` in RPC responses, streams raw Run events over SSE, and bridges non-delta events to WebSocket lifecycle summaries. Delta events are SSE-only.
- `core/automation/`, `core/subagents/`, channels, tools, and slash commands share the same `ChatRunManager`; they must not create parallel per-domain busy-session queues.
- `webui/` treats queue state and Run lifecycle truth as server-owned projections.

## Constraints & Gotchas

- Only one Run may be active per `(agent_id, session_id)`; Runs in different Sessions may execute in parallel. Session ids are globally unique UUIDs, so a project-scoped and a global session never collide on this key; `Run.project_id` records which anchor the session lives under for I/O, but does not affect dedup.
- Queue state, active Run lookup, completed Run lookup, and Run event replay windows are all in-memory and bounded. Process restart loses queue state and old timeline replay.
- `enqueue(...)` is not "always queued"; callers must handle the item future already being resolved to a running Run.
- Removing a queued item cancels its future. Updating a queued item replaces both executor and display preview, so build replacements through `ChatLoop.build_queue_update(...)` when user-visible chat content changes.
- The manager starts terminal bookkeeping. Normal executors should emit domain events and return or raise; they should not call `mark_completed`, `mark_failed`, or `mark_cancelled` themselves unless they deliberately own lifecycle completion.
- `Run.mark_failed(...)` is the single authoritative failure-log chokepoint: every executor (interactive, cron, channel, subagent) reaches it, so it logs there — expected `VBotError` at `warning` (no traceback), anything else at `error` with traceback (`vbot.runs`). Failure handlers elsewhere (e.g. `core/chat`'s `_persist_run_error`, subagent result folding) must not re-log the same failure.
- Cancellation is best effort. Late non-terminal provider/tool output is suppressed after `cancel_requested`, `Run.emit()` returns `None` for suppressed events, and already-emitted events remain replayable.
- Terminal events are the only events allowed after cancellation suppression starts. Terminal payloads include `timing` with `{ started_at, completed_at, duration_ms }`; duration uses a monotonic clock and timestamps are UTC ISO strings for display/persistence. `run_completed` may also include `usage`, kept separate from `timing`. `run_cancelled` also includes the optional `reason` field when one was supplied to `Run.request_cancel(...)`.
- WebSocket bridge code filters out SSE-only deltas and de-duplicates recently bridged Runs. Fix transport mapping in `server/rpc/event_bridge.py`, not in `core/runs/`.
