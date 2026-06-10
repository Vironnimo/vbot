# Handoff 3 — WebUI Chat / SSE / WebSocket bug review

Scope: read-only review of the chat surface (ChatView, chatRunStream, chatState, chatTimeline,
chatTimelinePresentation, ChatTimeline/ChatAssistantRun/ChatTimelineEntry), the transport layer
(`api.js`, `connectionState.js`, `/ws`, SSE), and the backing server/core pieces
(`server/app.py`, `server/events.py`, `server/rpc/event_bridge.py`, `server/rpc/chat_methods.py`,
`core/runs/runs.py`, `core/subagents/*`). Nothing was changed. Bugs only — structural/architecture
findings are discussed separately in chat.

Ordered by severity.

---

## B1 — WebSocket sequence regression after server restart: client silently receives no events

**Where:** [connectionState.js:24](webui/src/lib/connectionState.js#L24), [events.py:97-120](server/events.py#L97-L120)

The client keeps `lastSequence` in memory and passes it as `after_sequence` on every reconnect.
The server's `ServerEventBus` starts a fresh process at `_next_sequence = 1` and `subscribe()`
yields only events with `sequence > after_sequence`.

After a server restart (which agents trigger themselves — self-healing/restart is a core flow),
a long-lived tab reconnects with a stale, large `after_sequence` (e.g. 3000). The new bus emits
sequences 1, 2, 3 … — all filtered out, both by the server (`subscribe`) and by the client
(`onEvent` only raises `lastSequence`). The status pill shows **connected**, but the tab receives
zero lifecycle events until the new server has published more events than the old one ever did, or
until a full page reload.

Consequences: runs started by other accessors (Telegram, cron, sub-agents) never appear; sub-agent
dots never settle; queue projections never refresh; combined with B3 the session can get stuck.

**Fix direction:** add a bus epoch / server instance id to every event (or a hello frame on WS
open carrying the current max sequence). Client resets `lastSequence` when the epoch changes.

---

## B2 — SSE reconnect attempt counter never resets → live stream permanently gives up after 3 transient errors

**Where:** [chatRunStream.js:41-67](webui/src/lib/chatRunStream.js#L41-L67), [chatRunStream.js:238-277](webui/src/lib/chatRunStream.js#L238-L277)

`subscribeToRun` captures `retryAttempt` from its options; on `onError` it calls
`recoverRunStream(…, retryAttempt)` and resubscribes with `retryAttempt + 1`. Nothing ever resets
the counter when events flow again. The attempts accumulate **across the whole run**: three
transient drops hours apart (proxy idle timeouts, laptop sleep/wake, wifi blips) and the 4th error
hits `retryAttempt >= MAX_SSE_RECONNECT_ATTEMPTS`, the stream is closed for good and the UI falls
back to WS summaries only — no more deltas, no stdout/stderr, persistent error notice.

Also: the reconnect delay is a fixed 500 ms (no backoff), and a JSON parse error of a *single* SSE
event takes the same path (tears down and resubscribes the whole stream, consuming one attempt).

**Fix direction:** reset the attempt counter on the first successfully received event (or on
`onOpen`), and/or rely on EventSource's native reconnect + `Last-Event-ID` (the server already
honors it, [app.py:706-709](server/app.py#L706-L709)) instead of a custom capped loop.

---

## B3 — Session stuck in `running` forever when the terminal event is missed; history reload does not reconcile

**Where:** [chatState.js:104-138](webui/src/lib/chatState.js#L104-L138) (`loadHistory`), [chatRunStream.js:70-104](webui/src/lib/chatRunStream.js#L70-L104) (`attachRunStream`), [ChatView.svelte:316-338](webui/src/components/ChatView.svelte#L316-L338)

`loadHistory` keeps `status === 'running'` untouched when the session is marked running, and
`attachRunStream` is only called when the server response contains an `active_run`. If the server
says **no run is active** but the local state still says `running` (terminal event missed — see B1,
B2, WS buffer rollover, or a server restart that killed the run so no terminal event will *ever*
arrive), nothing ever resets the session:

- `New Session` stays blocked (`canCreateNewSession`),
- the header keeps showing the cancel control; cancelling fails (`RunNotFoundError`),
- the run block keeps its running spinner.

The only recovery is a full page reload (fresh `chatState`). Guaranteed repro: start a run, restart
the server, navigate within the tab — the session is permanently "running".

**Fix direction:** in `loadHistoryForSession`, when `history.active_run` is absent but
`isRunActive(sessionState)` is true, finish/reset the run state (status idle, clear currentRun and
streaming buffers).

---

## B4 — Sub-agent batch tracker leaks every non-blocking batch (entries are never pruned after the completion note)

**Where:** [tracker.py:160-203](core/subagents/tracker.py#L160-L203), [tracker.py:258-264](core/subagents/tracker.py#L258-L264)

`_prune_if_finished` requires every entry to be `complete` **and** `fetched`. The batch-completion
note marks `notification_sent = True` and embeds the full results of all *unfetched* entries — but
never marks them fetched. The tool description explicitly tells the model **not** to call
`subagent_result` afterwards, so for the standard non-blocking flow the entries stay unfetched
forever and the batch is never removed from `_batches`.

Result: one leaked `_SubAgentBatch` per parent run that used non-blocking sub-agents, including the
children's **complete final output strings** (`entry.result`), for the lifetime of the server
process.

**Fix direction:** mark the entries included in the completion note as fetched (or prune the batch
outright once the note has been sent).

---

## B5 — Stale "running" sub-agent markers after refresh: dot status has no durable fallback

**Where:** [chatTimelinePresentation.js:376-411](webui/src/lib/chatTimelinePresentation.js#L376-L411) (`subAgentDotStatus`), [ChatView.svelte:73](webui/src/components/ChatView.svelte#L73) (`subAgentRunStatuses`)

For a non-blocking spawn, the persisted tool result freezes the descriptor at
`status: "running"`. After a page refresh the only thing that can settle the dot is the WS replay
buffer (`run:`/`session:` status keys from replayed lifecycle events). The buffer holds 4096 events
([events.py:14](server/events.py#L14)); sub-agent-heavy workloads roll past that quickly, and after
a server restart the buffer is empty.

When the child's terminal event is not in the replay window, `externalSubAgentStatus` returns
nothing, `subAgentChildStatus` reads the frozen `"running"` descriptor, and the dot shows
**running forever**. `requestSubAgentResult` never fires either (it requires `dotStatus ===
'success'`), so the row also never gets its result. There is no self-healing query path (e.g.
checking the child run/session state via RPC).

**Fix direction:** when a sub-agent row claims `running` but no live status is known, verify
against the server (child `chat.history` already returns `active_run`; absence ⇒ settle the dot),
or persist the child's terminal status into the parent session (the batch-completion note's run
already exists — a durable per-tool status payload would close this).

---

## B6 — Sub-agent caches go stale when a child session is reused (`session_id` spawns)

**Where:** [ChatView.svelte:343-373](webui/src/components/ChatView.svelte#L343-L373) (`requestSubAgentResult`), [chatTimelinePresentation.js:991-1014](webui/src/lib/chatTimelinePresentation.js#L991-L1014) (`externalSubAgentStatus`), [chatRunStream.js:184-215](webui/src/lib/chatRunStream.js#L184-L215)

Several projections are keyed by `agentId::sessionId`, but `subagent` supports spawning into an
existing session repeatedly. Concretely:

1. **`subAgentResults` cache:** fetched once per `agentId::sessionId` and never invalidated. A
   second spawn into the same child session renders the **first run's** final output in its Result
   row. The error path caches `{ result: '' }` permanently, so a transient `chat.history` failure
   means that row never shows a result (no retry).
2. **`session:`-keyed status fallback:** a previous run's terminal status in the same child session
   makes a *new* queued/running spawn's dot show `success` (the `session:` lookup wins before the
   descriptor's own `queued`/`running` status is consulted).
3. **`sessionDuration:` fallback:** shows the latest run's duration on older spawn rows of the same
   session.

**Fix direction:** key result/status/duration lookups by child `run_id` (or the spawn's
`tool_call_id`) and use session-level keys only when no run id exists; invalidate the result cache
per spawn, not per session.

---

## B7 — Queued message stays visible as "queued" after it has started running

**Where:** [chatRunStream.js:164-182](webui/src/lib/chatRunStream.js#L164-L182) (`handleAppendedRunEvent`)

The queue projection is only re-synced from the server on **terminal** run events. When the
previous run finishes, the client calls `chat.queue_list`; if that RPC lands before the server's
`_drain_next` has started the queued item (the drain runs after the terminal event is emitted,
[runs.py:553-558](core/runs/runs.py#L553-L558)), the item is still in the list. The subsequent
`run_started` does **not** trigger a queue sync, so the started item remains displayed under
"Queued messages — waiting for the active run to finish" for the entire duration of its own run,
while its user message simultaneously appears in the timeline.

**Fix direction:** also `syncSessionQueue` on `run_started` (or have the server include
`queue_item_id` in the `run_started` payload so the client can remove the exact item).

---

## B8 — `/ws` endpoint never reads from the socket: disconnects are detected late, with log noise

**Where:** [app.py:276-286](server/app.py#L276-L286)

`websocket_events` only sends; it never calls `receive()`. A client disconnect is therefore only
noticed at the next `send_json`. Consequences:

- An idle disconnected client keeps its `ServerEventBus` subscriber registered until the next
  event is published (bounded, but every reconnect cycle stacks one zombie subscriber).
- Depending on the uvicorn/starlette versions, send-after-disconnect may raise
  `ClientDisconnected`/`RuntimeError` rather than `WebSocketDisconnect` — those are not caught
  here and surface as "Exception in ASGI application" tracebacks on every closed tab. (Verify with
  the pinned versions; the `/ws/logs` endpoint already handles this correctly with a concurrent
  `receive()` task, [app.py:621-654](server/app.py#L621-L654).)

**Fix direction:** mirror the `_stream_log_events` pattern (concurrent receive task) and broaden
the except clause.

---

## B9 — Pagination boundary artifacts: orphaned `tool` messages render as standalone "Tool result" messages

**Where:** [chatTimeline.js:204-288](webui/src/lib/chatTimeline.js#L204-L288) (`historyTimelineItems`)

History pages are message-window based (newest 100 / older 50). When the window starts in the
middle of an assistant turn, leading `role: "tool"` messages have no `activeAssistantRun` and fall
through to the generic branch: they render as standalone messages labeled "TOOL RESULT" with raw
JSON content as plain text — outside any run block. Similarly a leading `run_summary` is dropped,
so the first (partial) run block of the page loses its status/timing meta.

**Fix direction:** either snap page boundaries to user turns server-side, or have
`historyTimelineItems` open a synthetic assistant-run for leading `tool` messages.

---

## B10 — Unbounded client-side growth in long-lived tabs

**Where:** [chatRunStream.js:39](webui/src/lib/chatRunStream.js#L39) (`handledRunServerEventKeys`), [ChatView.svelte:73-74](webui/src/components/ChatView.svelte#L73-L74) (`subAgentRunStatuses`, `subAgentResults`), [chatState.js:210](webui/src/lib/chatState.js#L210) (`sessionState.runEvents`)

None of these are ever pruned while the tab lives:

- `handledRunServerEventKeys` grows by one key per WS run event, forever.
- `subAgentRunStatuses` accumulates `run:`/`session:`/`runDuration:`/`sessionDuration:` entries for
  **every** run of every session (not just sub-agents — `trackSubAgentRunStatus` records all runs).
- `sessionState.runEvents` accumulates all non-delta events of every run in a session for as long
  as the user stays on it (`loadHistory` only clears them on navigation when no run is active).
  Since `visibleTimelineItemsForRender` + `timelineSignature` re-project *everything* on every
  flush (every ≤33 ms while streaming), render cost grows with session age — a likely source of
  chat sluggishness in long sessions.

**Fix direction:** prune run events for terminal runs whose output is persisted (the
`dropPersistedInactiveLiveRuns` predicate already exists), cap the status maps, and consider
memoizing the per-run projection keyed by run id instead of re-walking all events.

---

## B11 — Replayed lifecycle events resubscribe SSE to old runs on page load (churn, transient error notices)

**Where:** [chatRunStream.js:290-319](webui/src/lib/chatRunStream.js#L290-L319) (`handleRunServerEvent`)

On a fresh page load the WS replays the whole retained buffer from sequence 0. Every replayed
`run_started` for the *displayed* session calls `attachRunStream`, which `startRun`s the old run
(briefly flipping the session to `running`) and opens an SSE subscription to
`/api/runs/<old-run-id>/events`. For completed runs this resolves itself when the replayed terminal
event arrives, but:

- if a replayed old run was pruned from `ChatRunManager` (>512 completed runs), the SSE returns
  404 → `recoverRunStream` runs its retry loop and can briefly show "The live stream closed.
  Reconnecting..." for a run that finished long ago;
- if the replay buffer rolled past the old run's terminal event, the session is left `running`
  (B3) even though everything is idle.

**Fix direction:** only attach to a replayed `run_started` when the run is plausibly still active
(e.g. no later terminal event for the same run id in the same replay batch), or filter the replay
server-side (see structural discussion).
