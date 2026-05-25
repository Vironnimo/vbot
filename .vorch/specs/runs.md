# Runs

Run lifecycle, cancellation, replayable timeline events, and in-memory busy-session queue coordination.

## Overview

`core/runs/` owns the provider-agnostic execution envelope around one active Session turn. A Run is not the ChatLoop itself and does not know provider, tool, Session storage, or WebUI details. It tracks lifecycle state, emits visible timeline events, supports replay/subscription for SSE, handles best-effort cancellation, and coordinates one active Run plus FIFO queued work per `(agent_id, session_id)`.

The chat loop creates and executes Runs. Server, channels, automation, tools, and WebUI-facing RPCs consume Run state and events through the public `core.runs` API.

## Data Model

- `RunStatus` — `running`, `completed`, `failed`, or `cancelled`.
- `RunEvent` — replayable event with `sequence`, `run_id`, `agent_id`, `session_id`, `type`, `payload`, and UTC `timestamp`.
- `Run` — active execution state with events, subscribers, cancellation callbacks, terminal status, final result, and final error.
- `QueuedRunItem` — one queued request with display content, executor, `internal` visibility flag, start future, and created timestamp.
- `ChatRunManager` — in-memory coordinator for active Runs, Run lookup/cancel, and per-session FIFO queues.
- `RunError`, `ActiveRunError`, `RunNotFoundError`, `RunCancelledError` — expected domain errors for caller mapping.
- `RunExecutor` — async callable that receives the `Run` object and returns the final result.

## Event Contract

Stable Run event constants live in `core.runs`:
- lifecycle: `run_started`, `run_completed`, `run_failed`, `run_cancelled`
- persisted/visible output: `user_message_persisted`, `reasoning`, `tool_call_started`, `tool_call_result`, `assistant_output`, `error_message_persisted`, `model_fallback_activated`, `compaction_completed`
- transient SSE-only deltas: `assistant_output_delta`, `reasoning_delta`, `tool_call_delta`, `tool_call_stdout`, `tool_call_stderr`

Every emitted event increments the Run-local `sequence`, including transient delta events. Subscribers can replay events after a sequence number and then follow live events until a terminal event.

`tool_call_started` payloads include both the raw call and display metadata:
`{ tool_call: { id, index, name, arguments }, display: { summary, hidden_argument_keys } }`.
The `display` object is produced by the tool registry and is safe for accessors
to render without re-inferring tool semantics from raw arguments.

## Interfaces

- `Run.emit(event_type, payload=None) -> RunEvent | None` appends and publishes a visible event unless the Run is already terminal or cancellation is suppressing non-terminal output.
- `Run.subscribe(after_sequence=0)` replays matching events and streams future events until terminal state.
- `Run.wait()` waits for terminal state, returns the result, re-raises failures, and raises `RunCancelledError` for cancelled Runs.
- `Run.request_cancel()` marks cancellation requested, runs registered cancellation callbacks, and cancels the background executor task.
- `Run.add_cancel_callback(callback)` registers cleanup work for active host/provider/tool work.
- `ChatRunManager.start(agent_id, session_id, executor)` starts immediately or raises `ActiveRunError` when that Session already has a running Run.
- `ChatRunManager.enqueue(...)` starts immediately when idle, otherwise stores a FIFO `QueuedRunItem`.
- `ChatRunManager.list_queued(...)`, `remove_queued(...)`, and `update_queued(...)` are the public queue controls used by server RPCs.
- `ChatRunManager.cancel(run_id)` and `cancel_by_session(agent_id, session_id)` request cancellation through the Run object.

## Cross-Domain Contracts

- `core/chat/` owns ChatMessage, provider/tool execution, retry/fallback behavior, and when to call Run APIs.
- `core/sessions/` owns persisted history. Runs do not read or write Session files.
- `server/` maps Run state to RPC, SSE, and WebSocket payloads. SSE streams Run events directly; WebSocket receives lifecycle summaries and excludes SSE-only delta events.
- `core/automation/` and `core/subagents/` start or queue work through the shared manager instead of creating their own queues.
- `core/channels/` subscribes to Runs for final replies and cancellation/failure outcomes.
- `webui/` treats queue state as server-owned and listens to Run SSE/WS contracts; it does not own lifecycle truth.

## Constraints & Gotchas

- Only one Run may be active per `(agent_id, session_id)`; parallel Runs in different Sessions are allowed.
- Queue state is intentionally in-memory only and is lost on process restart.
- Queued internal Runs must not appear in public queue list responses.
- Cancellation is best effort. Late non-terminal provider/tool output is suppressed, but already-emitted events remain replayable.
- `Run.emit()` returns `None` when suppression drops an event; callers must tolerate that.
- Terminal events are the only events allowed after cancellation suppression starts.
- All timestamps are UTC ISO 8601 strings with explicit offsets.