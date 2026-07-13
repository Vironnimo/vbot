# Runs

Run lifecycle, cancellation, replayable timeline events, and in-memory busy-session queue coordination.

## Overview

`core/runs/` owns the provider-agnostic execution envelope around one active Session turn. It does not own provider/tool execution, ChatMessage construction, Session persistence, server transport, or WebUI state; those domains consume Run state through `core.runs`. `ChatLoop` builds the `RunExecutor` and calls `ChatRunManager.start(...)` or `enqueue(...)`; the manager creates the `Run`, runs the executor in a background task, publishes lifecycle events, and drains queued work.

## Terms

Core term Run lives in `.vorch/GLOSSARY.md`.

### Waiting-Work Admission

**Definition:** A manager-owned reservation for an inbound item that has been accepted but cannot yet be represented as a queued Run, such as a channel attachment before its download. It atomically becomes a queued Run or is released when work starts or aborts.

**Not:** A second queue or a Run; it only accounts for capacity in the existing `ChatRunManager`.

## Data Model

- `RunStatus` — `running`, `completed`, `failed`, or `cancelled`.
- `RunEvent` — replayable timeline event with Run-local `sequence`, ids, `type`, JSON `payload`, and UTC ISO `timestamp`. It also carries `project_id` (the emitting Run's anchor, `None` for an identity run): `agent_id` stays bare, so a consumer that keys by the outside `agent@projekt` address rebuilds it from this field. Mirrored into `to_dict()` and the `/ws` bridge payload.
- `Run` — active execution state with separate `project_id` Session/address ownership and internal `working_project_id` working context, plus bounded replay/subscriber state, cancellation, result/error, and executor task. `working_project_id` is never public.
- `QueuedRunItem` — pending request with display preview, executor, `internal` flag, created timestamp, single-owner `future`, and internal immutable `working_project_id`. The queue key still owns only Session identity; public queue payloads omit the working Project.
- `ChatRunManager` — in-memory owner of active Runs, completed-Run lookup retention, per-session FIFO queues, cancellation, Run-start callbacks, and the bounded system-wide waiting-work accounting used by channel ingress.
- `RunExecutor` — async callable receiving the `Run` object and returning the final result. The manager translates returned results, raised errors, and cancellation into terminal Run state.
- `RunError`, `ActiveRunError`, `RunNotFoundError`, `RunCancelledError`, and `WaitingWorkLimitError` are expected domain errors for caller/RPC mapping.

## Event Contract

Most stable event constants live in `core.runs`: lifecycle (`run_started`, `run_completed`, `run_failed`, `run_cancelled`), output (`user_message_persisted`, `reasoning`, `tool_call_started`, `tool_call_result`, `assistant_output`, `error_message_persisted`, `model_fallback_activated`, `compaction_completed`), and SSE-only deltas (`assistant_output_delta`, `reasoning_delta`, `tool_call_delta`, `tool_call_stdout`, `tool_call_stderr`). Chat's `compaction_completed` payload is `{message, checkpoint, checkpoint_id, history_available}`; `message` is retained for compatibility. `subagent_session_started` is emitted on a Run timeline too, but its constant belongs to `core.subagents` because the payload is owned by the sub-agent domain.

Every emitted event increments the Run-local `sequence`, including transient deltas. Sequences are monotonic for that Run and are never reused when old events fall out of the retained replay window. `Run.subscribe(after_sequence=...)` replays retained events with larger sequence numbers, then follows live events until a terminal event; late subscribers can only replay the retained window.

Run event payload ownership stays with the domain that emits the event. `core/chat/` owns ChatMessage, tool-call, fallback, compaction, and error-message payloads; `core/subagents/` owns `subagent_session_started`; `server/` only maps Run events to SSE/WebSocket/RPC payloads and strips opaque provider metadata.

## Interfaces

- `Run.emit(event_type, payload=None, *, allow_after_cancel=False) -> RunEvent | None` appends and publishes an event unless the Run is terminal or cancellation suppression drops a non-terminal event. `allow_after_cancel=True` is the one deliberate suppression escape: an executor may still publish an event that finalizes output the user has already seen (the chat loop's preserved partial answer on cancel) — never new or late results. The `run_started` payload is `{"status": "running"}` with an optional `queue_item_id` when the run was started from a queued item.
- `Run.subscribe(after_sequence=0)` streams retained and future events until terminal state; lagging live subscribers are evicted instead of building unbounded queues.
- `Run.wait()` waits for terminal state, returns the executor result, re-raises failures, and raises `RunCancelledError` for cancelled Runs.
- `Run.request_cancel(reason=None)` marks cancellation requested, stores the optional `reason` on `Run.cancel_reason` for inclusion in the `run_cancelled` terminal payload, and schedules registered sync/async cancel callbacks. Once the manager task has entered its execution wrapper it is cancelled forcefully as before; a request made before that first step leaves the wrapper scheduled long enough to perform terminal cancellation bookkeeping without entering the caller's executor, so the Session is always released.
- `Run.add_cancel_callback(callback)` registers cleanup for active provider/tool/host work; callbacks registered after cancellation is already requested are scheduled immediately.
- `Run.register_tool_cancel(tool_call_id, callback)` registers a per-tool-call cancel callback without touching the run's overall cancellation state.
- `Run.cancel_tool_call(tool_call_id) -> bool` invokes the registered per-tool-call callback and marks the call user-cancelled; returns `False` for unknown or already-cancelled ids. It does NOT set `cancel_requested` and does NOT cancel the executor task — it is strictly separate from `request_cancel`.
- `Run.tool_call_cancelled(tool_call_id) -> bool` reports whether a specific tool call was user-cancelled via `cancel_tool_call`.
- `Run.clear_tool_cancel(tool_call_id)` removes the per-tool-call cancel registry entry.
- `Run.raise_if_cancelled()` lets executors stop between provider/tool steps once cancellation was requested.
- `ChatRunManager.start(agent_id, session_id, executor, *, project_id)` starts immediately or raises `ActiveRunError` when that Session already has a running Run. `enqueue(...)` starts immediately when idle and resolves the item future at once; otherwise it appends a FIFO `QueuedRunItem` for that session key. `project_id` enters the session key **and** is stored on the created `Run` for session I/O.
- **Waiting-work capacity is manager-owned.** At most `DEFAULT_WAITING_WORK_LIMIT` (32) tasks may wait system-wide; active Runs do not count. `enqueue` rejects a new normal queued Run at that ceiling. Channel ingress calls `reserve_waiting_work(scope, scope_limit)` before accepting raw work or downloading media; the reservation counts against the same ceiling and its per-scope limit. It is released when non-Run work begins, or transferred atomically through `enqueue(..., waiting_work_admission=...)`, so no second queue can over-admit. `release_waiting_work(...)` cleans up a reservation when processing fails or shuts down; `waiting_work_count()` is the central diagnostic count.
- **The dedup key is `(project_id, agent_id, session_id)`** (`SessionKey`). The project anchor is part of the key because `session.create` accepts caller-chosen session ids, so identity `builder` and project `builder@vbot` can both own a session named `main` — the two sessions must never block, cancel, or guard each other (the recall FTS index was scoped by project for exactly this case). Every key-touching method (`start`, `enqueue`, `active_run`, `cancel_by_session`, `list_queued`, `remove_queued`, `update_queued`, `has_activity_for_agent`, `has_activity_for_session`) takes a **required** keyword-only `project_id` (`None` = the identity anchor), so no caller can silently fall into the wrong scope. Every RPC surface knows the project from its parsed `agent@projekt` address; the queue RPCs (`chat.queue_*`) parse the address too.
- `ChatRunManager.list_queued(...)`, `remove_queued(...)`, and `update_queued(...)` are raw queue controls. They include internal items; public RPC filtering belongs in `server/rpc/chat_methods.py`.
- `ChatRunManager.get(run_id)`, `active_run(...)`, `cancel(run_id, reason=None)`, and `cancel_by_session(..., reason=None)` are the lookup/cancellation surface used by server RPCs, slash commands, channels, tools, and sub-agent cleanup. Normal Stop surfaces pass `reason="user"`; the Chat continuation layer uses that explicit reason to distinguish user Cancel from provider/system interruption.
- `ChatRunManager.has_activity_for_agent(agent_id, *, project_id)` reports whether one `(project, agent)` pair owns any active or queued work. Scoping by project keeps same-named agents apart: identity-agent deletion checks `project_id=None`, project removal checks each session-owning team agent under its own `project_id` — an active run of identity `builder` no longer blocks removing an unrelated project whose team also has a `builder`, and vice versa.
- `ChatRunManager.has_activity_for_working_project(project_id)` spans active Runs and queued items regardless of Session ownership; Project removal uses it to protect accepted Rooted-Agent and Config-Agent work.
- `ChatRunManager.has_activity_for_session(agent_id, session_id, *, project_id)` reports whether one Session owns an active or queued Run, keyed on the exact `(project_id, agent_id, session_id)` triple both the active-run and queue maps use. `session.delete` calls it to refuse removing a Session with work in flight.
- `ChatRunManager.active_runs() -> list[Run]` returns a snapshot (fresh list) of all entries in `_active_by_session` whose `status == RunStatus.RUNNING`. Public accessor mirroring `active_run(...)`; used by the `/ws` handshake to include active runs in the `connection_ready` snapshot.

## Cross-Domain Contracts

- `core/chat/` owns provider calls, tool execution, message persistence, retry/fallback behavior, and which Run events to emit. New chat execution paths should call the manager instead of constructing `Run` directly.
- `core/sessions/` owns durable history. Run timelines are process-local replay buffers and are not a substitute for JSONL Session history.
- `server/` exposes `Run.events` in RPC responses, streams raw Run events over SSE, and bridges non-delta events to WebSocket lifecycle summaries. Delta events are SSE-only.
- `core/automation/`, `core/subagents/`, channels, tools, and slash commands share the same `ChatRunManager`; they must not create parallel per-domain busy-session queues. Channel ingress reservations are manager state, not a separate scheduler, and are transferred to the normal Run FIFO when a turn is busy.
- `webui/` treats queue state and Run lifecycle truth as server-owned projections.

## Constraints & Gotchas

- Only one Run may be active per `(project_id, agent_id, session_id)`; Runs in different Sessions (including a project and an identity session sharing agent/session ids) execute in parallel. `Run.project_id` mirrors the key's project dimension and is what the executor's session I/O reads.
- Queue state, ingress reservations, active Run lookup, completed Run lookup, and Run event replay windows are all in-memory and bounded. The shared waiting-work ceiling is 32 across queued Runs and reservations; process restart loses queue state and old timeline replay.
- `enqueue(...)` is not "always queued"; callers must handle the item future already being resolved to a running Run. A caller that awaits the bare future and is cancelled abandons that queued work; do not shield or replace the future-cancellation removal without introducing another explicit ownership contract.
- Removing a queued item cancels its future. Updating a queued item replaces both executor and display preview, so build replacements through `ChatLoop.build_queue_update(...)` when user-visible chat content changes.
- The manager starts terminal bookkeeping. Normal executors should emit domain events and return or raise; they should not call `mark_completed`, `mark_failed`, or `mark_cancelled` themselves unless they deliberately own lifecycle completion.
- `Run.mark_failed(...)` is the single authoritative failure-log chokepoint: every executor (interactive, cron, channel, subagent) reaches it, so it logs there — expected `VBotError` at `warning` (no traceback), anything else at `error` with traceback (`vbot.runs`). Failure handlers elsewhere (e.g. `core/chat`'s `_persist_run_error`, subagent result folding) must not re-log the same failure.
- Cancellation is best effort. Late non-terminal provider/tool output is suppressed after `cancel_requested`, `Run.emit()` returns `None` for suppressed events, and already-emitted events remain replayable.
- Cancelling immediately after `start(...)` returns must still move the Run to `cancelled`, resolve `Run.wait()`, remove the active-session entry, and drain queued work. Do not cancel a newly created manager task before its execution wrapper has entered the lifecycle `try/finally`; Python otherwise skips that coroutine body entirely and strands the Session in `running`.
- After cancellation suppression starts, only terminal events and an explicit `allow_after_cancel` emit (the preserved-partial finalization — see `Run.emit`) pass. Terminal payloads include `timing` with `{ started_at, completed_at, duration_ms }`; duration uses a monotonic clock and timestamps are UTC ISO strings for display/persistence. `run_completed` may also include `usage`, kept separate from `timing`. `run_cancelled` also includes the optional `reason` field when one was supplied to `Run.request_cancel(...)`. Executors may fill the `Run.terminal_payload_extras` dict before returning/raising; the manager merges it into the terminal payload of every outcome alongside `timing`. The chat loop supplies `session_usage` on every outcome and, for an interrupted visible Run, a public `continuation` summary `{checkpoint_id, origin_run_id, latest_run_id, cause, state, user_initiated, can_continue}`. Causes are `user`, `provider`, `network`, `timeout`, `process_restart`, or `internal`; user Cancel is the only cause with `can_continue=false`. A successful complete assistant result omits/clears that summary. Continuation stays orthogonal to `RunStatus`, so a preserved partial response can retain interrupted work without adding another public Run status.
- WebSocket bridge code filters out SSE-only deltas and de-duplicates recently bridged Runs. Fix transport mapping in `server/rpc/event_bridge.py`, not in `core/runs/`.
