# Plan: Robust run-lifecycle truth for the WebUI (transport epoch, connect snapshot, status reconciliation)

**Goal:** The WebUI never silently loses run-lifecycle truth again — after server restarts, missed
events, buffer rollovers, or page refreshes, run/sub-agent markers settle correctly, sessions never
get stuck in `running`, and the timeline no longer depends on replaying historical events.

**Context:** A read-only review of the chat/SSE/WebSocket stack (full bug list:
[handoff3.md](handoff3.md), remaining backlog: [STRUCTURE.md](STRUCTURE.md)) found one root cause
behind most recurring problems: **the client reconstructs truth from event replays instead of
querying it.** This plan introduces (1) a transport epoch + connect snapshot and (2) explicit
status reconciliation, fixing bugs **B1, B2, B3, B5, B7, B11** from handoff3.md.

---

## ⚠ Execution notes — read first

- **The root-cause analysis below is already verified by code reading. Do NOT try to reproduce
  the bugs live** (no server restarts, no real WebSocket disconnect experiments, no waiting for
  buffer rollovers). Every bug has a deterministic unit-test recipe in its phase — regression
  tests simulate the triggering event sequences directly against the functions involved.
- Read at session start (per CLAUDE.md): `.vorch/PROJECT.md`, `.vorch/GLOSSARY.md`. Per phase,
  read the specs listed in its `read:` field.
- Quality gates before each commit: `python scripts/quality.py <changed backend paths>` and
  `python scripts/quality-frontend.py <changed frontend paths>`. Tests are written **with** the
  feature, in the same phase.
- Frontend is Svelte 5 + JavaScript (no TypeScript). No new user-visible strings are expected in
  this plan; if you add one, it must go through `t(...)` (`webui/src/lib/i18n.js`).
- The protocol change is breaking by design — **no legacy compatibility branches** (PROJECT.md
  convention). Server and WebUI change together in this plan.
- Spec maintenance is part of each phase, not a separate step.

---

## Verified current behavior (the facts the plan builds on)

**How the pieces talk today:**

- `ServerEventBus` ([server/events.py](server/events.py)) publishes envelopes
  `{ sequence, type, payload, timestamp }`. `sequence` starts at 1 **per process** and the bus
  retains the last 4096 events. `subscribe(after_sequence=N)` first replays every retained event
  with `sequence > N`, then streams live events, always filtering `sequence > after_sequence`.
- The `/ws` endpoint ([server/app.py:276-286](server/app.py#L276-L286)) parses `after_sequence`
  from the query string and just forwards `event_bus.subscribe(...)` output.
- Run lifecycle events reach the bus via `server/rpc/event_bridge.py`:
  `_publish_run_events` subscribes to each Run and maps `RunEvent`s through
  `_server_event_from_run_event` ([event_bridge.py:148-165](server/rpc/event_bridge.py#L148-L165)).
  **Important:** the WS summary payload contains `output` (the run event's payload) **only** for
  event types in `RUN_OUTPUT_EVENT_TYPES`; `run_started` is NOT in that set, so a `run_started`
  WS summary carries no run payload — only ids/sequence/timestamp.
- The client (`webui/src/lib/connectionState.js`) keeps `lastSequence` in memory, raises it on
  every event (`event.sequence > state.lastSequence`), and passes it as `after_sequence` on every
  reconnect. A fresh page load connects with `after_sequence = 0`, so the server replays the whole
  retained buffer.
- `webui/src/lib/chatRunStream.js` converts WS summaries back into run events
  (`runEventFromServerEvent`, [chatRunStream.js:331-358](webui/src/lib/chatRunStream.js#L331-L358)
  — note it builds the payload from `payload.output`), dedups them
  (`runServerEventKey`), and on every `run_started` for the *displayed* session calls
  `attachRunStream`, which opens an SSE subscription to `/api/runs/<run_id>/events`.
- `ChatRunManager` ([core/runs/runs.py](core/runs/runs.py)) holds active runs in
  `_active_by_session`, retains up to 512 terminal runs in `_runs`, and emits `run_started` with
  payload `{"status": "running"}` inside `_execute`. Queued items start via `_drain_next`
  ([runs.py:560-583](core/runs/runs.py#L560-L583)) **after** the previous run's terminal event was
  emitted.

**The five bugs this plan fixes:**

- **B1 — sequence regression after restart.** After a server restart the bus restarts at
  sequence 1, but a long-lived tab reconnects with its old large `after_sequence` (e.g. 3000).
  Server-side `subscribe` filters everything (`1,2,3… ≤ 3000`), client-side `onEvent` also ignores
  lower sequences. The tab shows "connected" but receives **nothing** until a full page reload.
  vBot agents trigger restarts themselves, so this is a normal flow, not an edge case.
- **B3 — stuck `running` session.** `loadHistory`
  ([chatState.js:104-138](webui/src/lib/chatState.js#L104-L138)) deliberately keeps run state when
  `status === 'running'`, and `attachRunStream` is only called when `chat.history` returned an
  `active_run`. If the terminal event was missed (B1, SSE gave up, buffer rolled, server restart
  killed the run so no terminal event will *ever* exist), **nothing resets the session**: New
  Session stays blocked (`canCreateNewSession`), the cancel control points at a dead run, only a
  full reload helps.
- **B5 — sub-agent dots stuck on `running` after refresh.** A non-blocking spawn's persisted tool
  result freezes `status: "running"`. After refresh, the only healer is the WS replay buffer
  feeding `subAgentRunStatuses`. If the child's terminal event rolled out of the 4096-event window
  (or the server restarted), `subAgentDotStatus`
  ([chatTimelinePresentation.js:376-411](webui/src/lib/chatTimelinePresentation.js#L376-L411))
  falls back to the frozen descriptor → dot runs forever, and `requestSubAgentResult` never fires
  (it requires `dotStatus === 'success'`).
- **B7 — started queue item still displayed as queued.** The queue projection is only re-synced on
  **terminal** events ([chatRunStream.js:164-182](webui/src/lib/chatRunStream.js#L164-L182)). The
  terminal-triggered `chat.queue_list` can win the race against the server's `_drain_next`; the
  later `run_started` does not sync the queue, so the started item stays visible as "queued" for
  its entire run.
- **B11 — replay churn on page load.** The full-buffer replay re-delivers `run_started` for runs
  that finished long ago. For the displayed session each one calls `attachRunStream` → `startRun`
  (briefly flips the session to running) → opens SSE to an old run. If that run was pruned
  (>512 retained), the SSE 404s and `recoverRunStream` shows "The live stream closed.
  Reconnecting..." for a run that finished long ago; if its terminal event rolled out of the
  buffer, the session stays running (B3).

---

**Scope:**
- In: event-bus epoch, `connection_ready` hello frame with active-run snapshot, epoch-scoped
  replay, client epoch handling, snapshot consumption, history reconcile, sub-agent dot self-heal,
  `queue_item_id` in `run_started`, spec updates.
- Out: everything in [STRUCTURE.md](STRUCTURE.md) (streaming-projection consolidation, memory
  bounds, SSE reconnect policy, bugs B2/B4/B6/B8/B9/B10). No provider/chat-loop changes, no
  persistence-format changes.

**Open decision (default chosen):** snapshot via hello frame (server push on WS open) rather than
a new `run.active_list` RPC. Rationale: avoids a fetch race between snapshot and first live events;
`/ws` stays server-push-only (no client→server WS messages are introduced).

---

## Workstream 1 — Transport epoch + connect snapshot

### Phase 1.1: Server — epoch + hello frame + scoped replay
**Goal:** `/ws` clients can detect a new server generation; fresh connects get a snapshot instead
of a historical replay.
**read:** [.vorch/specs/server.md, .vorch/specs/runs.md]

Tasks:

1. **Bus epoch** — files: [server/events.py, tests/server/test_events.py]
   - `ServerEventBus.__init__`: create `self._epoch = uuid.uuid4().hex`; expose read-only
     properties `epoch` and `last_sequence` (`self._next_sequence - 1`).
   - `publish(...)`: include `"epoch": self._epoch` in every event dict (alongside `sequence`).
   - `subscribe(...)` keeps its signature; epoch checking happens in the endpoint (next task),
     not in the bus.

2. **`/ws` handshake** — files: [server/app.py, tests/server/test_websocket.py,
   tests/server/test_app.py]
   - Parse a new optional `epoch` query param next to `after_sequence`.
   - After `websocket.accept()`, build and send a hello frame **directly** via
     `websocket.send_json` (NOT through `event_bus.publish` — it is connection-specific and must
     not enter the retained buffer; `ALLOWED_SERVER_EVENT_TYPES` stays unchanged):
     ```json
     {
       "type": "connection_ready",
       "epoch": "<bus epoch>",
       "last_sequence": <bus.last_sequence>,
       "active_runs": [
         { "run_id": "...", "agent_id": "...", "session_id": "...",
           "status": "running", "sse_url": "/api/runs/<run_id>/events" }
       ]
     }
     ```
     `active_runs` comes from a new `ChatRunManager.active_runs()` (task 3). No `sequence` field —
     the client's `lastSequence` logic must not be affected by the hello frame.
   - Replay rule replacing the current unconditional `subscribe(after_sequence=...)`:
     - client sent `epoch` matching the bus epoch **and** `after_sequence > 0` → subscribe with
       the client's `after_sequence` (reconnect resume, unchanged semantics);
     - otherwise (no epoch, stale epoch, or `after_sequence == 0`) → subscribe with
       `after_sequence = last_sequence_at_hello` (live-only; the snapshot covers state).
     - Ordering note: read `last_sequence` *before* `await websocket.send_json(hello)`; events
       published during that await have higher sequences and sit in the retained deque, so the
       subsequent `subscribe(after_sequence=last_sequence_at_hello)` replays them — no gap.

3. **Active-run accessor** — files: [core/runs/runs.py, tests/core/runs/test_runs.py]
   - Add `ChatRunManager.active_runs(self) -> list[Run]`: all `_active_by_session` values with
     `status == RunStatus.RUNNING`. Public, documented method (mirrors `active_run(...)`).

4. **Specs** — files: [.vorch/specs/server.md, .vorch/specs/runs.md]
   - server.md: WS contract (hello frame shape, epoch query param, replay rules).
   - runs.md: `active_runs()` in the Interfaces list.

**Done when:**
- Test: connect without params → first frame is `connection_ready`; pre-connect bus events are
  **not** delivered afterwards; events published after connect are.
- Test: connect with `after_sequence=3&epoch=<current>` after publishing 5 events → events 4–5
  replayed (resume still works).
- Test: connect with `after_sequence=3000&epoch=stale-or-missing` → hello + only new live events
  (regression for **B1**, server half).
- Test: `connection_ready.active_runs` lists a running run with correct `sse_url` and omits
  terminal runs.

### Phase 1.2: Client — epoch handling in connectionState
**Goal:** the client survives sequence resets and exposes the snapshot to the app.
**read:** [.vorch/specs/webui.md]

Tasks:

1. **connectionState** — files: [webui/src/lib/connectionState.js,
   webui/src/lib/__tests__/connectionState.test.js]
   - `createConnectionState()`: add `epoch: ''`.
   - In the internal `onEvent` wrapper, **before** the sequence bookkeeping: if
     `event.type === 'connection_ready'` → set `state.epoch = event.epoch ?? ''` and
     `state.lastSequence = Number.isFinite(event.last_sequence) ? event.last_sequence : 0`,
     then forward the event to `handlers.onEvent` and return (do not run the
     `event.sequence > lastSequence` update on it).
   - `connect(...)`: pass `epoch: state.epoch` through to `subscribeServerEvents` options.

2. **api.js** — files: [webui/src/lib/api.js, webui/src/lib/__tests__/api.test.js]
   - `subscribeServerEvents` / `buildWebSocketUrl`: append `epoch` query param when non-empty
     (same pattern as `after_sequence`).

3. **App.svelte routing** — files: [webui/src/App.svelte,
   webui/src/components/__tests__/App.test.js]
   - `handleServerEvent`: route `connection_ready` into a new `$state connectionSnapshot`
     (the whole frame) passed to ChatView as a prop. Keep `runServerEvents` for normal lifecycle
     events. (Do not stuff the hello frame into `runServerEvents` — it has no
     `payload.run_id`/`run_event_sequence`, so `runServerEventKey` would drop it.)

**Done when:**
- Test (regression for **B1**, client half): state with `lastSequence = 3000` receives
  `connection_ready` with a different epoch and `last_sequence: 0`; a following event with
  `sequence: 1` reaches `handlers.onEvent` and bumps `lastSequence` to 1.
- Test: same-epoch hello does not lower `lastSequence` below a later event's sequence handling
  (i.e. resume path unchanged).

### Phase 1.3: Client — consume snapshot instead of replay
**Goal:** displayed-session SSE attachment and sub-agent run statuses come from the snapshot; the
replay-attach heuristic disappears.
**read:** [.vorch/specs/webui.md, .vorch/specs/subagents.md]

Tasks:

1. **Snapshot application** — files: [webui/src/lib/chatRunStream.js,
   webui/src/lib/__tests__/chatRunStream.test.js (new file)]
   - Add `applyConnectionSnapshot(snapshot)` to the object returned by `createChatRunStream`:
     - For each `snapshot.active_runs[i]`: call `updateSubAgentRunStatuses` with
       `run:<run_id> = 'running'` and `session:<agent_id>::<session_id> = 'running'` (same shape
       `trackSubAgentRunStatus` produces).
     - If `isDisplayedSession(agent_id, session_id)`: `ensureSessionState` +
       `attachRunStream(sessionState, { run_id, status: 'running', sse_url, events: [] })`.
   - `handleRunServerEvent`: keep appending events and statuses, **keep** the
     `run_started → attachRunStream` branch (it is still how a run started *while the tab is
     open* in a displayed session gets its SSE — e.g. queued items draining, runs started from
     other accessors). What disappears is the replay that used to feed it stale `run_started`s;
     no code change needed here beyond what Phase 2.3 adds.
2. **ChatView wiring** — files: [webui/src/components/ChatView.svelte,
   webui/src/components/__tests__/ChatView.test.js]
   - New prop `connectionSnapshot`; `$effect` that calls
     `runStream.applyConnectionSnapshot(connectionSnapshot)` once per distinct snapshot object
     (track the last applied reference, same pattern as `pendingSubAgentNavigation` handling).
3. **Spec** — files: [.vorch/specs/webui.md]
   - Replace the replay-from-0 paragraphs (the ones describing WS retained-buffer replay
     re-injecting completed runs) with the snapshot contract.

**Done when:**
- Test: snapshot with one active run for the displayed session → exactly one SSE subscription via
  the injected `subscribeRunEvents`, session state running.
- Test: snapshot with active runs only in *other* sessions → zero SSE subscriptions, but
  `subAgentRunStatuses` got `run:`/`session:` = running entries.
- Test (regression for **B11**): a `connection_ready` with empty `active_runs` plus *no* replayed
  `run_started` events results in zero subscriptions and an idle session (assert no
  `setActionError` call).

---

## Workstream 2 — Durable status truth + reconciliation

### Phase 2.1: History reconcile (fixes B3)
**read:** [.vorch/specs/webui.md, .vorch/specs/chat.md]

Tasks:

1. **chatState helper** — files: [webui/src/lib/chatState.js,
   webui/src/lib/__tests__/chatState.test.js]
   - Add `resetStaleRun(sessionState)`: set `status = CHAT_STATUS_IDLE`,
     `streamStatus = CHAT_STATUS_IDLE`, `currentRun = null`, clear `streamingItems`,
     `streamingRunEvents`, `seenStreamingEventKeys`, reset `streamingPhase = 0`. Leave
     `runEvents` and `messages` untouched (the reloaded history is about to be the displayed
     source; dropping `currentRun` makes `selectTrackedRunTimelineSource` inactive, so history
     renders).
2. **Subscription closer** — files: [webui/src/lib/chatRunStream.js]
   - Export `closeSubscriptionFor(sessionKey)` on the returned object (wraps the existing
     internal `closeRunSubscription` + `clearPendingReconnect`).
3. **ChatView reconcile** — files: [webui/src/components/ChatView.svelte,
   webui/src/components/__tests__/ChatView.test.js]
   - In `loadHistoryForSession`, before the `await rpc('chat.history', ...)`, capture
     `const staleRunId = sessionState.currentRun?.runId ?? ''`.
   - After `loadHistory(...)`: if `!history.active_run && isRunActive(sessionState) &&
     sessionState.currentRun?.runId === staleRunId` → `resetStaleRun(sessionState)` +
     `runStream.closeSubscriptionFor(sessionState.key)`. The `staleRunId` guard prevents
     resetting a run that genuinely started between request and response (the next WS
     `run_started` re-establishes running state anyway — losing it here would be a regression).

**Done when:**
- Test (regression for **B3**): session state with `status: 'running'`, `currentRun` set; feed a
  history response **without** `active_run` → session idle, `currentRun === null`,
  `canCreateNewSession(...)` true.
- Test: same setup but history response **with** `active_run` → unchanged behavior
  (`attachRunStream` called, still running).
- Test: history response without `active_run` but `currentRun.runId` changed during the await →
  no reset.

### Phase 2.2: Sub-agent dot self-heal (fixes B5)
**Depends on:** Phase 2.1 (ChatView.svelte overlap — run sequentially).
**read:** [.vorch/specs/subagents.md, .vorch/specs/sessions.md]

Mechanism (verified): child sessions persist `run_summary` messages (role `run_summary`, carrying
`run_id`, `status`, `timing`) which `chat.history` returns, and `chat.history` returns
`active_run` when the child is still running. That is the durable truth source — no new RPC
needed.

Tasks:

1. **Verification request path** — files: [webui/src/components/ChatView.svelte,
   webui/src/components/__tests__/ChatView.test.js]
   - Add `verifySubAgentStatus(agentId, sessionId, runId)` next to `requestSubAgentResult`,
     with a once-per-key guard map (key: `runId || agentId::sessionId`, per ChatView instance):
     - call `chat.history` with `{ agent_id, session_id, limit: 20 }`;
     - if the response has `active_run` → record `run:<active_run.run_id>` /
       `session:<agentId>::<sessionId>` = `'running'` via `updateSubAgentRunStatuses` (verified
       running — and the live attach is not needed; WS will deliver its terminal);
     - else find the **last** message with `role === 'run_summary'` (matching `run_id` if one was
       passed, otherwise the last one); map its `status`
       (`completed|failed|cancelled` — same values `statusFromRunEvent` produces) into the same
       `run:`/`session:` keys, plus `runDuration:`/`sessionDuration:` from
       `timing.duration_ms` when finite; fallback status when no summary exists: `'completed'`.
   - On error: release the guard so a later attempt can retry (unlike the
     `subAgentResults` error cache — see handoff3 B6 — do not poison the cache).
2. **Trigger from the run component** — files:
   [webui/src/components/chat/ChatAssistantRun.svelte,
   webui/src/lib/chatTimelinePresentation.js,
   webui/src/lib/__tests__/chatTimelinePresentation.test.js]
   - New pure helper `subAgentNeedsStatusVerification(tool, dotStatus, subAgentStatuses)` in
     chatTimelinePresentation.js: true when `dotStatus === 'running'` **and** neither
     `run:<run_id>` nor `session:<agent>::<session>` exists in `subAgentStatuses` (i.e. the
     "running" belief comes only from the frozen persisted descriptor, not from any live event
     or snapshot).
   - In ChatAssistantRun, mirror the existing `subAgentResultFetchTargets` `$derived` +
     `$effect` pattern with the new predicate, calling a new `onVerifySubAgentStatus` callback
     prop (wired through ChatTimeline → ChatView like `onRequestSubAgentResult`).
   - Settling the dot to `success` makes the existing `subAgentShouldFetchResult` effect fire,
     so the Result row fills in without further changes.
3. **ChatTimeline pass-through** — files: [webui/src/components/ChatTimeline.svelte]
4. **Spec** — files: [.vorch/specs/webui.md] (document the self-heal lookup and its guard).

**Done when:**
- Test (helper): frozen-descriptor tool row + empty `subAgentStatuses` → needs verification;
  same row with `session:...: 'running'` present → does not.
- Test (regression for **B5**, jsdom/harness): render a sub-agent row whose descriptor says
  running with empty statuses; stub `chat.history` to return a `run_summary` with
  `status: 'completed'`, `timing.duration_ms: 4200` → dot settles to success, time label shows
  the child duration, result fetch was issued.
- Test: stubbed `chat.history` returning `active_run` → dot stays running, no result fetch, no
  second verification for the same key.

### Phase 2.3: Queue truth on run start (fixes B7)
**read:** [.vorch/specs/runs.md, .vorch/specs/webui.md]

Tasks:

1. **Core: thread `queue_item_id` into `run_started`** — files: [core/runs/runs.py,
   tests/core/runs/test_runs.py]
   - `_start_run_locked(...)`: new optional keyword `queue_item_id: str | None = None`; store it
     on the run (private attr, e.g. `run._started_from_queue_item_id`) or pass it through to
     `_execute`.
   - `_execute(...)`: emit `run_started` with payload
     `{"status": "running", "queue_item_id": <id>}` when present, unchanged otherwise.
   - Call sites: `_drain_next` passes `item.item_id`; `enqueue`'s start-immediately branch also
     passes `item.item_id` (the caller received a queued-item handle either way); plain
     `start(...)` passes nothing.
2. **Bridge: expose the payload on the WS summary** — files: [server/rpc/event_bridge.py,
   tests/server/rpc/ (existing event-bridge tests)]
   - `_server_event_from_run_event`: for `RUN_STARTED_EVENT`, include
     `payload["output"] = _remove_opaque_provider_metadata(event.payload)`. **This step is
     load-bearing:** today `run_started` is not in `RUN_OUTPUT_EVENT_TYPES`, so without it the
     WS summary silently drops `queue_item_id` and the client never sees it
     (`runEventFromServerEvent` rebuilds the payload from `payload.output`).
3. **Client: remove the started item** — files: [webui/src/lib/chatRunStream.js,
   webui/src/lib/chatState.js, webui/src/lib/__tests__/chatRunStream.test.js]
   - In `handleAppendedRunEvent` (runs for both SSE and WS paths): if
     `event.type === 'run_started'` and `event.payload?.queue_item_id` →
     `removeQueuedMessage(sessionState, event.payload.queue_item_id)` (already exported from
     chatState.js). Keep the terminal-event `syncSessionQueue` as the consistency backstop.
4. **Specs** — files: [.vorch/specs/runs.md, .vorch/specs/webui.md]
   - runs.md: `run_started` payload now optionally carries `queue_item_id`.
   - webui.md: queue projection note (optimistic removal on `run_started`, server list remains
     source of truth).

**Done when:**
- Test (core): `enqueue` onto a busy session, finish the active run → the drained run's
  `run_started` event payload contains the queued item's id; a `start(...)` run's payload does
  not contain the key.
- Test (client, regression for **B7**): session state with a queued item; append a `run_started`
  event carrying that `queue_item_id` → `sessionState.queue` no longer contains it, without any
  `chat.queue_list` round-trip.

---

## Phase 3: Cleanup pass (after both workstreams)

**read:** [.vorch/specs/webui.md]

- Re-evaluate the replay-compensation code whose trigger path the snapshot removed:
  `dropPersistedInactiveLiveRuns` ([chatTimeline.js:429-449](webui/src/lib/chatTimeline.js#L429-L449))
  existed because the WS replay re-injected completed runs next to the active one. With
  replay-from-0 gone, completed prior runs only enter `runEvents` while the tab is open (which is
  legitimate live state). **Keep** the active-run anchor dedup in `selectTrackedRunTimelineSource`
  (still needed for refresh-during-run via `history.active_run.events`). Remove
  `dropPersistedInactiveLiveRuns` only if all its Vitest cases can be re-expressed as
  no-longer-possible input; otherwise keep it and just note in webui.md that it is a safety net.
  — files: [webui/src/lib/chatTimeline.js, webui/src/components/__tests__/ChatTimeline.test.js]
- Sweep `.vorch/specs/webui.md` for now-obsolete workaround documentation (replay buffer
  paragraphs, rollover caveats that the snapshot/self-heal made moot). — files:
  [.vorch/specs/webui.md]

**Done when:** full `python scripts/quality.py server core/runs` and
`python scripts/quality-frontend.py webui/src` pass; no spec paragraph describes removed behavior.

---

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Hello/subscribe gap: events published while `send_json(hello)` awaits | Med | Med | Read `last_sequence` before sending hello; `subscribe(after_sequence=that_value)` replays the retained in-between events (they are in the deque). Covered by an explicit test in Phase 1.1. |
| Another `/ws` consumer relies on full replay | Low | Med | Desktop is a pywebview shell of the same WebUI (no own WS client). Before Phase 1.1, `grep -rn "after_sequence"` across `cli/`, `desktop/`, `tests/` and adjust the hits deliberately. |
| Phase 2.1 reconcile races a genuinely starting run | Med | Low | `staleRunId` guard (reset only when `currentRun.runId` is unchanged across the await); a real new run re-asserts running via its `run_started`. |
| `chat.history` limit 20 misses the relevant `run_summary` in a busy child session | Low | Low | Acceptable: fallback is `'completed'`, which unblocks the UI; the result fetch shows the real last output. |
| Hello frame on every reconnect re-applies the snapshot and re-attaches SSE | Med | Low | `applyConnectionSnapshot` goes through `attachRunStream`, which already no-ops when the same run/sse_url is subscribed (`alreadySubscribed` check). ChatView applies each snapshot object once. |
