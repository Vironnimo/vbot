# Runs

Run lifecycle, cancellation, replayable timeline events, and in-memory busy-session Queue coordination, including steering.

## Overview

`core/runs/` owns the provider-agnostic execution envelope around one active Session turn. It does not own provider/tool execution, ChatMessage construction, event payloads, Session persistence, server transport, or WebUI state; those domains consume Run state through `core.runs`. `ChatLoop` builds the `RunExecutor` and calls `ChatRunManager.start(...)` or `enqueue(...)`; the manager creates the `Run`, runs the executor in a background task, publishes lifecycle events, settles terminal state, and drains queued work.

## Terms

Core terms Run, Queue and Steer live in `.vorch/GLOSSARY.md`.

### Waiting-Work Admission

A reservation for inbound work that is accepted but cannot yet become a queued Run, such as a Channel attachment before its download; it either becomes a queued Run or is released (`runs/queue-and-admission.md` -> Waiting work). Not a second Queue or a Run: it only counts against the manager's waiting-work capacity.

### Run Admission Guard

A block on new Run admission for one Session, Agent or Project scope, which a destructive workflow such as archive, move or delete holds while it changes that scope and can take only while the scope has no active or queued work (Interfaces). Not a cancellation mechanism or a persistence lock: the workflow still makes its storage changes through its own domain's APIs.

## Data Model

- `RunStatus` - `running`, `completed`, `failed`, `cancelled`, or `interrupted`. `interrupted` is a terminal unsuccessful outcome for a Run whose execution ended without a trustworthy completion after bounded recovery; it is distinct from both an ordinary failure and user cancellation.
- `RunKind` - stable producer category carried by every Run (`run.py`), independent of `internal` and Agent-activity policy. `calendar` remains only in stored history: Cron jobs bound to calendar events start `cron` Runs (`automation.md`). `UNATTENDED_RUN_KINDS` (the three reflection kinds and `librarian`) with `is_unattended_run_kind(kind)` is the one definition of background learning Runs: Tool dispatch passes the Run kind on `ToolContext.run_kind`, `skill_manage` restricts what unattended Runs may change (`skills.md` -> Background Writer), and Statistics excludes Sessions whose Runs are all unattended from Skill use and gives their Runs the `reflection` origin (`statistics.md`).
- `RunEvent` - replayable timeline event with Run-local `sequence`, ids, `type`, JSON `payload`, and UTC ISO `timestamp`. It also carries `project_id` (the emitting Run's anchor, `None` for an identity run) and `run_kind`: `agent_id` stays bare, so a consumer that keys by the outside `agent@projekt` address rebuilds it from these fields. Mirrored into `to_dict()` and the `/ws` bridge payload. Events from Runs excluded from Agent activity also carry `contributes_to_agent_activity: false`; the default `true` is omitted.
- `Run` - active execution state. `project_id` is the Session/address anchor that the executor's Session I/O reads; `working_project_id` is internal working context; `run_kind` is immutable. `contributes_to_agent_activity` (default `True`) controls only Agent/Session attention status; neither it nor `run_kind` changes execution, admission, cancellation, persistence, timeline visibility, or diagnostics. Admission's `completion_activity` can replace it at finish (`runs/queue-and-admission.md`). `iteration_count` is the canonical Agentic Loop count (`chat/run-execution.md`), projected into Run responses, active snapshots and terminal payloads.
- `ChatRunManager` - in-memory owner of active Runs, completed-Run lookup retention, per-session FIFO Queues, Run Admission Guards, cancellation, Run start/finish callbacks, and bounded system-wide waiting-work accounting for channel ingress.
- `RunExecutor` - async callable receiving the `Run` object and returning the final result. The manager translates returned results, raised errors, and cancellation into terminal Run state. Expected domain errors for caller/RPC mapping are the `RunError` family in `run.py`.
- Run and Queue ids are `run_`/`que_` plus 16 lowercase base32 characters (`PROJECT.md` -> Object IDs).

## Events

Run event payload ownership stays with the domain that emits the event; `server/` only maps Run events to SSE/WebSocket/RPC payloads and strips opaque provider metadata. Payload homes: Message, Tool, streaming-delta, Provider status, fallback and error events -> `chat/run-execution.md` -> Agentic progression and events; `model_step_usage` -> `chat/usage.md`; `run_change_stats` -> `tools/change_tracker.md`; `compaction_*` -> `compaction/run-coordination.md` -> Run events; `speech_playback` -> `tools/speech.md`; `subagent_session_started` -> `subagents.md`; lifecycle and terminal payloads -> `runs/lifecycle.md` -> Terminal payloads.

Every emitted event, transient deltas included, increments the Run-local `sequence` (monotonic, never reused); `Run.subscribe(after_sequence=...)` replays retained events, then follows live events until a terminal event, evicting lagging subscribers (`runs/events.md`).

**Finished-Run replay retention.** A running Run retains its newest 4096 events; the terminal event shrinks the window to the settled ending (the terminal event plus the unbroken run of non-transient events before it, at most 16), which a late subscriber receives as an ordinary replay head. Whatever needs the full timeline subscribes while the Run runs; durable history serves everything else. Up to 512 terminal Runs stay addressable by id (`runs/events.md` -> Sequencing and replay).

## Interfaces

- `Run.emit(event_type, payload=None, *, allow_after_cancel=False) -> RunEvent | None` appends and publishes an event unless the Run is terminal or cancellation suppression drops it (then it returns `None`): cancellation is best effort, and after `cancel_requested` only terminal events and an explicit `allow_after_cancel` emit pass. That flag is the one deliberate escape, for output the user has already seen (the chat loop's preserved partial answer on cancel) or a result the cancel could no longer keep from being stored (`compaction_completed` for a committed checkpoint) - never new or late results.
- `Run.wait()` returns the executor result, re-raises failures, raises `RunCancelledError` for cancelled Runs, and re-raises `RunInterruptedError` for interrupted Runs while the partial result remains on the Run.
- `Run.request_cancel(reason=None, *, initiator=None)` cascades still-active per-Tool cancel callbacks, then schedules the Run's cancel callbacks (`add_cancel_callback`), which the manager drains within one five-second budget before terminal cancellation (`runs/lifecycle.md` -> Cancellation). Per-Tool cancellation (`cancel_tool_call`) stays local to one call and never cancels the Run. `Run.raise_if_cancelled()` lets executors stop between provider/tool steps.
- `ChatRunManager.start(address, executor, *, admission=...)` starts immediately or raises `ActiveRunError` for a busy Session; `enqueue(...)` starts when idle or stores a FIFO `QueuedRunItem`. `admission` (`RunAdmission`) bundles the immutable origin, activity, work-correlation and Session-generation decisions a drained Run keeps (`runs/queue-and-admission.md`). `enqueue(...)` is not "always queued": callers must handle the item future already being resolved to a running Run, and a caller that awaits the bare future and is cancelled abandons that queued work.
- **The dedup key is the `SessionAddress`** `(project_id, agent_id, session_id)` (born in `core.sessions`): one active Run per address; Runs of different addresses execute in parallel. The project anchor is part of the key because `session.create` accepts caller-chosen session ids, so identity `builder` and project `builder@vbot` can both own a session named `main` that must never block, cancel, or guard each other. `start`, `enqueue` and `session_admission_guard` take the address as one value; every public lookup method (`active_run`, `cancel_by_session`, `list_queued`, `remove_queued`, `update_queued`, `has_activity_for_session`) takes scalar `agent_id`/`session_id` plus a **required** keyword-only `project_id` (`None` = the identity anchor), so no caller silently falls into the wrong scope; RPC surfaces take it from the parsed `agent@projekt` address.
- `session_admission_guard(*session_addresses)` atomically guards one or more exact `SessionAddress` identities (no raw tuples); Agent Takeover supplies source and destination together. `agent_admission_guard(agent_id, *, project_id)` guards all Sessions owned by one Agent in one anchor. `project_admission_guard(project_id)` guards both Project-anchored Sessions and Identity-Agent Runs whose internal working Project selects that Project. Acquisition refuses existing active/queued work or an overlapping guard; while held, `start`/`enqueue` raise `RunAdmissionBlockedError`.
- `has_activity_for_agent(agent_id, *, project_id)`, `has_activity_for_working_project(project_id)` and `has_activity_for_session(...)` are execution/admission snapshots, including Runs outside Agent activity; they are neither UI-attention queries nor synchronization primitives. A busy check followed by awaited lifecycle work is racy: destructive Session/Agent/Project workflows hold the matching Run Admission Guard, never a check-then-archive/move/delete on `has_activity_*`.
- `get(run_id)`, `active_run(...)`, `cancel(run_id, reason=None, *, initiator=None)` and `cancel_by_session(...)` are the shared lookup/cancellation surface. Normal Stop surfaces pass `reason="user"`; `reason` carries semantics, `initiator` only tells the log who asked (`runs/lifecycle.md` -> Cancellation). Stopping a Run never cascades to its Sub-Agents or background commands; only Stop all does (`subagents.md` -> Conventions -> Stop). `aclose()` closes admission and cancels all work at shutdown (`runs/lifecycle.md` -> Cancellation).
- **Waiting-work capacity is manager-owned:** at most 32 tasks wait system-wide across queued Runs and Channel ingress reservations, which transfer atomically into the normal Run FIFO (`runs/queue-and-admission.md` -> Waiting work).

## Cross-Domain Contracts

- Runtime constructs `ChatRunManager(persistence=sessions)` before creating Chat consumers (standalone managers may omit it). Admission is persisted before executor entry; terminal commit precedes publication and owner completion notifications. `RunPersistence.finish_run(run, status, payload, *, completion_reason)` receives why a Run stopped early: the `cancel_reason` of a cancelled Run, the `RunInterruptedError` cause of an interrupted one, otherwise `None`; Chat derives the next request's interruption notice from it (`chat/run-execution.md` -> Interruption). Cancellation cannot change an outcome once its terminal commit begins; commit and notification failures: `runs/lifecycle.md` -> Terminal settlement.
- `Run.add_completion_finalizer(callback)` registers cleanup that needs the chosen outcome while history remains writable: the manager freezes cancellation, invokes finalizers (Chat: Extension `run_end` and its notes), then commits the terminal summary; failures are logged and reported with completion-notification errors without stranding the Run. Post-commit `add_completion_observer` is the boundary for Reflection and other consumers that need durable history.
- Run owns transient accessor controls: Compaction request state (consumed only by Chat's Compaction coordinator) and Tool-registered background callbacks. `run_controls_changed` carries the authoritative projection, also in Run responses and reconnect snapshots with a sequence so stale replies cannot restore retired actions; terminal Runs expose no actions.
- `core/chat/` owns provider calls, tool execution, message persistence, retry/fallback behavior, and which Run events to emit. New chat execution paths should call the manager instead of constructing `Run` directly.
- Sessions records the admitted Run with its `run_kind` and any Extension execution owner in one admission transaction (`ChatSessionManager.start_run`); `Run.wait_admitted()` returns once it committed and raises the Run's terminal error (or `RunCancelledError`) when the Run ends first.
- `core/sessions/` owns durable history. Run timelines are process-local replay buffers and are not a substitute for canonical SQLite Session history. `session.delete` and Agent Takeover hold Run Admission Guards across their awaited archive/move and follow-up metadata writes; a Session `write_lock` alone cannot prevent a later Run from recreating a moved or archived address.
- `server/` exposes `Run.events` in RPC responses (for a finished Run only its settled ending), streams raw Run events over SSE, and bridges non-delta events to WebSocket lifecycle summaries; high-volume deltas are SSE-only, while the low-rate `provider_heartbeat` is also bridged so an observing Parent keeps child Run liveness. The bridge also de-duplicates recently bridged Runs; fix transport mapping in `server/rpc/event_bridge.py`, not in `core/runs/`.
- `core/automation/`, `core/subagents/`, channels, tools, and slash commands share the same `ChatRunManager`; they must not create parallel per-domain busy-session queues. Channel ingress reservations are manager state, not a separate scheduler.
- `webui/` treats queue state and Run lifecycle truth as server-owned projections.

## Constraints & Gotchas

- Queue state, ingress reservations, active Run lookup, completed Run lookup, and Run event replay windows are all in-memory and bounded; process restart loses Queue state and old timeline replay. `connection_ready.queues` reports the new process's complete public Queue state for client reconciliation; it does not persist or resume discarded work.
- The manager alone settles terminal state. Executors emit domain events and return or raise (`RunInterruptedError` for an interruption); they do not call `mark_completed`, `mark_failed`, `mark_cancelled` or `mark_interrupted` unless they deliberately own lifecycle completion. Every executor exit settles terminal state before the Session is released. The one `mark_*` call writes the Run's one terminal log line (`vbot.runs`); executors fill its facts and never log the outcome (`runs/lifecycle.md` -> Terminal log line). A Run a previous process left running is settled and logged by Session restart recovery instead (`sessions.md`).

## Internal source routing

`runs.py` owns `ChatRunManager`, admission guards and Queue lifecycle; the package exports records and constants directly from `run.py`; `run.py` one Run's events, cancellation, terminal state and the shared execution records; `_timeline.py` `RunTimeline`. Public imports go through `core.runs`.

## References

Read these only when your task matches - not by default.

- Changing Run event constants, sequencing, replay retention, transient event types, the shared event stream (`core/event_stream.py`) or `RunTimeline` -> `runs/events.md`
- Changing Run or per-Tool cancellation, manager shutdown (`aclose`), terminal settlement, terminal payloads or the terminal log line -> `runs/lifecycle.md`
- Changing Queue items, Queue edits or drain, steering selection, `RunAdmission`, waiting-work capacity or Extension execution ownership -> `runs/queue-and-admission.md`
