# Session Activity

The server-owned activity of each Session - `working`, `waiting` or `idle` - and its work span, which every Accessor reads instead of reconstructing it from Run events.

## Overview

`core/activity/` (`SessionActivity` in `activity.py`, re-exported by `core.activity`) aggregates four owners that do not know each other: Runs (`runs.md`), background shell commands (`tools/shell.md` -> CommandManager contract), notices held by completion delivery (`automation/completion-delivery.md` -> Pending wakes) and Sub-Agent links and forwarding (`subagents/forwarding.md` -> Session activity). It is an owner of its own because Runs sit below the others and must not depend on them, and none of the others holds all four. Runtime builds it in bootstrap phase 8 (`session_activity`) and closes it right after readiness is withdrawn (`runtime/lifecycle-steps.md`). The server publishes it (`server/events-and-reconnect.md` -> Session activity); the WebUI, the TUI and the tray render it (`webui.md`, `cli.md`).

Not owned here: whether a Run counts toward Agent activity (`RunAdmission.contributes_to_agent_activity`, `runs.md`; this module only reports it), unread completion (persisted by Sessions; Accessors show unread only while `idle`), Run durations in Statistics, Usage and Debug (they stay per Run), and stopping work (Stop all, `subagents.md` -> Conventions).

## Requirements (user decisions 2026-10-21)

1. A Session is not finished while anything pending will wake it again by itself. The server owns one three-state activity that every Accessor reads; none reconstructs it from events.
   - `working`: an active Run or queued input.
   - `waiting`: no Run, but Sub-Agents whose answers go to this Session (link `reply_target == "parent"`) that work or wait themselves (transitively), including the gap between a child Run's end and its notice reaching completion delivery; background commands with armed delivery that are not services, including the window between their end and their notice reaching completion delivery; waking notices held by completion delivery that are neither read nor `suppress_run`.
   - `idle`: none of that.
2. Finished means finished: the unread marker and the tray's Run-completed toast appear only once the Session is `idle`, not at every Run end. Failure toasts stay immediate.
3. Terminals never count toward `waiting` (notices they already submitted count like any notice). A background command stops counting once any process of its process tree listens on a TCP port (a service: dev server, database); the classification is sticky. Watchers without a port keep counting (accepted gap; Stop all ends them).
4. `waiting` looks almost like `working` (amber: both mean not finished), told apart subtly (WebUI: a ring instead of a filled dot) and by the tooltip, which names what is pending.
5. While a Session is `waiting`, its stop control is Stop all (`chat.stop_all`), which ends all work that would wake it.
6. The work span is the user-facing duration of the whole piece of work. Live counters outside the transcript (TUI status row, tooltips) show it, also while waiting; the end notice names the total. The WebUI Run footer keeps each Run's own duration and adds `(Waiting)` to the latest Run's while the Session waits; no second elapsed-time display. Statistics, Usage and Debug stay per Run. The span is process-local, not persisted.

## Terms

### Work span
From the confirmed leave of `idle` to the next confirmed `idle`, across all Runs and waiting time of the Session; reported as `work_started_at` and, with the move to `idle`, as `finished_work`. Not a Run's duration.

### Pending wake
Something that will start a Run in the Session by itself, without user input: a counted Sub-Agent, background command or held result (`waiting_for`). Not a Queue item, which makes the Session `working`.

### Service command
A background command one of whose processes listens on a TCP port; from then on it is no pending wake, though it still delivers its result when it ends (`tools/shell.md` -> CommandManager contract).

## Interfaces

- `SessionActivity(*, runs, commands, completions, subagents, clock=..., monotonic=..., sleep=...)`: the sources are Protocols (`RunSource`, `CommandSource`, `CompletionSource`, `SubAgentSource`) implemented by `ChatRunManager`, `CommandManager`, `TriggerService` and `SubAgentCoordinator`; the clocks and `sleep` are test seams.
- `state(address) -> SessionActivityState` (as last published; `IDLE` for a Session not followed) and `snapshot()` (every non-idle Session with its state) read under a lock and may be called from any thread.
- `add_changed_callback(callback)` calls `callback(address, state, finished)` on the Event Loop for each published change and returns the unsubscribe function; `finished` (`FinishedWork(started_at, finished_at, duration_ms)`) comes only with a move to `idle`. Callback failures are logged.
- `SessionActivityState(state, work_started_at, waiting_for, contributes_to_agent_activity)`: `waiting_for` (`WaitingFor(subagents, commands, results)`) is all zero unless `waiting`; `contributes_to_agent_activity` is False only while `working` with every active and queued Run excluded from Agent activity; `waiting` always counts.
- `aclose()` unsubscribes and cancels pending recomputes and settle periods; nothing is published afterwards.
- Source contracts it relies on: the Run-finished callback runs after the next queued item started (`runs/lifecycle.md` -> Terminal settlement); `CommandManager.pending_wakes` and its change callback (`tools/shell.md`); `TriggerService.pending_completion_wakes` and `add_completion_changed_callback`; `SubAgentCoordinator.forwarding_pending`, `add_forwarded_callback` and `parent_link`.
- Server contract (event payload, hello field, list-row fields, order after Run terminal events): `server/events-and-reconnect.md` -> Session activity.

## Conventions

- Event-driven, never polled: a Run start or finish, a command change, a completion-delivery change and a finished forwarding mark the Session dirty; one worker task per Session recomputes until nothing is dirty. A published change of a Session (or its finished forwarding) also recomputes its Parent.
- A move into `working` or `waiting`, and any change of `waiting_for` or of the contributes flag, publishes at once. A move to `idle` starts a settle period (`IDLE_SETTLE_SECONDS`, 2 s), and only a recheck after it publishes `idle`, so the short hand-offs (command end -> notice submission, child Run end -> forwarding, Run end -> next queued Run) never flicker. Leaving `idle` inside the settle period continues the same span.
- `finished_at` is when the Session became idle, not when that was confirmed; `duration_ms` comes from the monotonic clock.
- Sub-Agents: a child counts for the Parent its link names while the link says `reply_target == "parent"` and the child is not published `idle`, has an active or queued Run, or still forwards a Run. Candidate links are read afresh on every recompute through `parent_link` (off the Event Loop), since a user input can redirect a child's answers. A child whose link cannot be read counts, since `idle` would be premature (WARNING once, INFO on recovery). Each followed child's Parent is cached until the child is idle with nothing left to forward.
- Logging: published changes are DEBUG (`Session activity changed (...)`); a command classified as a service is INFO at its owner (`tools/shell.md`).
- Tests drive a real `ChatRunManager` with fakes for the other sources and a controlled clock and sleep; no real waits.

## Constraints & Gotchas

- Process-local: a restarted server starts every Session `idle` (no Run, background command or held notice survives a restart as wake work), and every Accessor replaces its state with the new hello. The Runtime builds Session activity inside the synchronous `bootstrap`, after Cron, Channels and command recovery were started but before the Event Loop runs any of their tasks, so no change can precede its subscription; keep it in that synchronous stretch.
- No Queue hook: input queued behind an active Run triggers no recompute, so `contributes_to_agent_activity` can stay False after a contributing item queued behind an excluded Run until the next event.
- A user input that redirects a child's answers to the user takes effect for the Parent at the child's next change; until then the Parent stays `waiting`.
- A child stays published non-idle through its own settle period, so its Parent reaches `idle` up to one settle period later per tree level.
- Shutdown closes Session activity right after readiness is withdrawn, so the work stopped at shutdown publishes no `idle` and no `finished_work`. Synchronous `stop()` skips it; marks without a running Event Loop are ignored.
- Keep the names apart: `activity_status` is the background job list and `session.activity_list` lists completed Sessions; Run events' `contributes_to_agent_activity` is the per-Run flag. `has_active_run` on list rows stays a pure active-Run flag (Live voice reads it), and `CommandManager.has_pending_delivery` keeps counting services (Goals read it).

## Source and tests

`core/activity/activity.py` (`__init__.py` re-exports); service detection `core/tools/_service_detection.py` and `process_tree.listening_pids`; wiring `core/runtime/_bootstrap.py`, `_shutdown.py`; server `server/rpc/event_bridge.py`, `server/_app_lifecycle.py`, `server/_streams.py`, `server/rpc/session_methods.py`. Tests: `tests/core/activity/test_session_activity.py` (states, settle, span, contributes flag, transitive Sub-Agents, reply targets, forwarding gap), source contracts in `test_command_manager.py`, `test_automation_completion.py` and `test_runs_lifecycle.py`, server contracts in `test_event_bridge.py`, `test_websocket_handshake.py` and `test_session_methods.py`.
