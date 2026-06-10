# STRUCTURE.md — WebUI chat / transport: structural backlog

Findings from the 2026-06-10 read-only review of the chat surface and SSE/WebSocket transport that
are **not** covered by [PLAN.md](PLAN.md) (transport epoch, connect snapshot, status
reconciliation). Bug numbers refer to [handoff3.md](handoff3.md), which has the detailed analysis
and code references. Items here are candidates to pick up after the plan lands — each section is
independently shippable.

---

## 1. Consolidate the three parallel streaming projections

**Problem:** Live run output is modeled three times in `chatState.js`:

- `sessionState.runEvents` — stable (non-delta) run events,
- `sessionState.streamingRunEvents` — compressed delta events (`_streamChunkCount`,
  `_streamLatestSequence`, `_streamingPhase` bookkeeping),
- `sessionState.streamingItems` — a second delta projection (typed items with `phase`).

Only the first two are rendered: `visibleTimelineItemsForRender` passes
`includeStreamingAssistantAndReasoning: false` / `includeStreamingToolCalls: false`, which filters
`streamingItems` out entirely — the `item.type === 'streaming'` branch in `ChatTimelineEntry.svelte`
(and `shouldRenderStreamingItem`) is effectively dead in the render path. `streamingItems` still
feeds `visibleTimelineItems` (tests/legacy selector) and the sequence bookkeeping in
`highestContiguousRunEventSequence`.

**Direction:** one reducer per `run_id` that folds deltas and stable events into a single live-run
projection (the shape `buildLiveAssistantRunItem` already produces), with replay-sequence tracking
as part of that reducer. Delete `streamingItems`, the dead render branch, and one of the two
"merge consecutive deltas" implementations (`appendCompressedStreamingRunEvent` vs.
`appendTextStreamingItem` duplicate each other).

**Files:** `webui/src/lib/chatState.js`, `webui/src/lib/chatTimeline.js`,
`webui/src/components/chat/ChatTimelineEntry.svelte`, matching `__tests__`.

---

## 2. Bound client-side growth and render cost (includes bug B10) — ✅ resolved 2026-06-10

Shipped (`webui/src/lib/clientCaches.js`; spec note in `.vorch/specs/webui.md` → State Flows):

- `handledRunServerEventKeys` is a capped insertion-ordered key set (cap > App.svelte's bounded
  500-event `runServerEvents` window).
- `subAgentRunStatuses` and `subAgentResults` are LRU-capped; evicting a `run:`/`session:` status
  key releases its verification guard so still-rendered rows can self-heal again.
- `loadHistory` prunes retained `runEvents` of non-active runs whose output the fresh history
  fully persists (event-level counterpart of `dropPersistedInactiveLiveRuns`).
- `chatTimeline` memoizes the projected `assistant_run` item of terminal runs per session+run, so
  the ≤33 ms streaming flush only rebuilds the active run's projection.

Accepted residual: `runEvents` of the viewed session still grows with genuinely viewed scrollback
between history loads — that is retained content, and its per-flush render cost is now bounded by
the memoization.

---

## 3. SSE reconnect policy (includes bug B2)

**Problem:** `chatRunStream.js` implements its own capped reconnect loop (3 attempts, fixed
500 ms, counter **never resets** on successful data → a long run permanently loses its live stream
after three transient drops, hours apart). A JSON parse error of a single event takes the same
teardown path and consumes an attempt. Meanwhile the server already supports native EventSource
resume: SSE `id:` lines + `Last-Event-ID` (`server/app.py` → `_replay_after_sequence`).

**Direction:** prefer native EventSource reconnection and treat the custom loop as fallback only;
at minimum reset the attempt counter on the first received event and add backoff. Treat
single-event parse errors as log-and-skip, not stream failure. Keep the
"run no longer running → stop reconnecting" guard.

**Files:** `webui/src/lib/chatRunStream.js`, `webui/src/lib/api.js`
(`subscribeRunEvents` error semantics), matching `__tests__`.

---

## 4. Remaining bugs from handoff3.md (standalone fixes, not transport-related)

| Bug | Summary | Sketch |
|---|---|---|
| **B4** | `SubAgentBatchTracker` leaks every non-blocking batch: after the completion note, entries are complete-but-unfetched forever (the note even tells the model not to fetch), `_batches` grows for the server's lifetime, retaining full child outputs | Mark noted entries as fetched / prune the batch once `notification_sent` and all complete (`core/subagents/tracker.py`) |
| **B6** | Sub-agent caches keyed by `agentId::sessionId` go stale when a child session is reused: second spawn shows the first run's cached result; stale `session:` status can show `success` for a queued spawn; error path caches an empty result permanently | Key result/status/duration by child `run_id` (or spawn `tool_call_id`), session keys only as last resort; evict/retry the error cache (`ChatView.svelte`, `chatTimelinePresentation.js`) — partially superseded by PLAN Phase 2.2 |
| **B8** | `/ws` endpoint never calls `receive()`: client disconnects detected only on next send; zombie subscribers until next publish; send-after-disconnect may raise non-`WebSocketDisconnect` exceptions → ASGI tracebacks (verify against pinned uvicorn/starlette) | Mirror the `_stream_log_events` concurrent-receive pattern (`server/app.py`) |
| **B9** | History page boundaries mid-turn: leading `role: "tool"` messages render as standalone "TOOL RESULT" plain-text messages; a leading `run_summary` is dropped | Open a synthetic assistant-run for leading tool messages in `historyTimelineItems`, or snap page boundaries to user turns server-side (`webui/src/lib/chatTimeline.js` / `server/rpc/chat_methods.py`) |

(B1, B3, B5, B7, B11 are handled by [PLAN.md](PLAN.md).)

---

## 5. Smaller observations (no action committed)

- **`/ws` is unscoped:** every client receives lifecycle events for all agents/sessions (channels,
  cron, sub-agents). Fine single-user-local, but each tab pays processing cost for everything;
  if it ever becomes a problem, scope subscriptions by agent or let the client filter earlier
  (before the bounded `runServerEvents` list in `App.svelte`).
- **Spec weight as a smell:** `.vorch/specs/webui.md` spends multiple long paragraphs documenting
  replay-dedup workarounds (`dropPersistedInactiveLiveRuns`, anchor dedup, rollover caveats). After
  PLAN.md lands, treat any new workaround paragraph of that kind as a signal the transport design
  is being fought rather than used.
- **`historyTimelineItems` heuristics:** assistant-run grouping is inferred from message order
  (`previousVisibleRole`, `followsAssistant`). A persisted `run_id` on assistant/tool messages
  (sessions domain) would make grouping exact and remove the content-match fallback in
  `findMatchingHistoryUserIndex`.
