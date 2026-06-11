# Plan: Sub-agent rows show the child's latest tool call while running

**Goal:** While a spawned sub-agent run is running, its row in the parent chat shows the name of the child's most recent tool call instead of the static prompt preview, so the user sees that work is happening.

**Context:**

Today a spawn row renders `Sub-agent · Agent ID: <id> · <truncated prompt>` ([ChatAssistantRun.svelte:250-257](../../webui/src/components/chat/ChatAssistantRun.svelte), preview from `subAgentPreview` / `subAgentToolLabel` in [chatTimelinePresentation.js:387,1024](../../webui/src/lib/chatTimelinePresentation.js)). The prompt is frozen for the whole child runtime, so nothing visibly moves.

The event flow needed for live tool activity **already exists end to end — no backend change**:

- Every Run (including sub-agent child Runs — they start through the shared `ChatRunManager`) is bridged onto the `/ws` bus by the run-started callback registered in `server/app.py:353` (`_register_run_event_bridge`) → `server/rpc/event_bridge.py`. Non-delta events incl. `tool_call_started` go out as `run_output` server events carrying the child's `agent_id`/`session_id`/`run_id` and `output.tool_call.name` (payload emitted in `core/chat/tool_dispatch.py:66`).
- Client: `App.svelte` queues `run_output` into the bounded `runServerEvents` list (500) → ChatView → `chatRunStream.handleServerEvents` → `handleRunServerEvent` → `appendRunEvent` into the child session state → `handleAppendedRunEvent` → `trackSubAgentRunStatus(event)`.
- `trackSubAgentRunStatus` ([chatRunStream.js:209](../../webui/src/lib/chatRunStream.js)) today records only `run:` / `session:` / `runDuration:` / `sessionDuration:` / `queueRun:` keys into ChatView's LRU-capped (2000) `subAgentRunStatuses` projection; `tool_call_started` falls through (`statusFromRunEvent` returns `''`).

So the change is purely: record the latest tool name into the existing projection, add one lookup helper, render it in the row.

**Decisions (made here so the builder doesn't re-decide):**

- **Key discipline mirrors B6** (see `.vorch/specs/webui.md` state flows): write both `runTool:<run_id>` and `sessionTool:<agent_id>::<session_id>`; the lookup resolves strictly by run id when `subAgentEffectiveRunId` yields one, session-scoped only for run-id-less rows — exactly the structure of `subAgentRunDurationMs` ([chatTimelinePresentation.js:246](../../webui/src/lib/chatTimelinePresentation.js)).
- **On `run_started`, reset `sessionTool:<agent>::<session>` to `''`** so a reused child session never shows the previous run's last tool on run-id-less rows.
- **Display rule:** only on `subagent` spawn rows (not `subagent_result`), only while the row's dot is `running`, and only when a name was recorded; otherwise the existing prompt preview stays (also: before the first child tool call, the prompt shows). After terminal state the row reverts to the prompt preview — the projection is in-memory and empty after a refresh anyway, so this keeps live and reloaded states consistent.
- **No i18n changes:** the tool name is a technical identifier rendered raw, like tool rows render `te-fn` today. No new copy.
- Writing `runTool:` for *all* runs (the parent's own tool calls also pass through the tracker via SSE) is fine and matches how `run:`/`runDuration:` keys already behave — only sub-agent rows read these keys, stale entries age out of the LRU.

**Scope:**

- In: frontend tracking + helper + row rendering, Vitest coverage, `.vorch/specs/webui.md` update, ChatView cache-cap comment touch-up.
- Out: backend/event-contract changes; showing tool arguments or descriptions; activity on `subagent_result` rows; persisting the last tool name across reloads.

**Phases:**

### Phase 1: Projection + lookup helper

- Record last tool name in `trackSubAgentRunStatus` ⚡ *parallel with next task* — read: [.vorch/specs/webui.md, .vorch/specs/runs.md], files: [webui/src/lib/chatRunStream.js, webui/src/lib/__tests__/chatRunStream.test.js, webui/src/components/ChatView.svelte]
  - Restructure `trackSubAgentRunStatus` so `tool_call_started` is handled (it currently early-returns when `statusFromRunEvent` is empty). For `tool_call_started` with a non-empty `event.payload?.tool_call?.name`: write `runTool:<run_id>` (when `event.run_id`) and `sessionTool:<agent_id>::<session_id>` (when both ids exist). For `run_started`: additionally write `sessionTool:<agent_id>::<session_id> = ''`.
  - ChatView.svelte change is comment-only: the cache-cap note "~5 entries per run" (around line 90) becomes ~7.
  - Tests: a `run_output` server event with `run_event_type: tool_call_started` records both keys; `run_started` resets the session-scoped key; events without a tool name write nothing new.
- Add `subAgentLastToolName(tool, subAgentStatuses)` ⚡ — files: [webui/src/lib/chatTimelinePresentation.js, webui/src/lib/__tests__/chatTimelinePresentation.test.js]
  - Returns `''` unless `toolNameForRunTool(tool) === 'subagent'`. Resolves `runTool:<run_id>` strictly when `subAgentEffectiveRunId` yields a run id (a session-keyed entry must NOT apply then); falls back to `sessionTool:<agent>::<session>` only for run-id-less rows. Mirror `subAgentRunDurationMs`.
  - Tests: run-scoped strict resolution, session fallback for run-id-less rows, `''` for `subagent_result` tools and unknown keys.

### Phase 2: Row rendering + spec

- Render the activity in the spawn row — files: [webui/src/components/chat/ChatAssistantRun.svelte, webui/src/components/chat/__tests__/ChatAssistantRun.test.js]
  - In the sub-agent summary line: derive `lastToolName = dotStatus === 'running' ? subAgentLastToolName(child, subAgentStatuses) : ''`; when non-empty, render it inside the existing `.subagent-preview` span (add a modifier class, e.g. `subagent-activity`, for optional styling — no new CSS required) instead of `subAgentPreview(child)`; otherwise keep the prompt preview exactly as today.
  - Tests (jsdom): running row + `runTool:` entry → tool name visible, prompt hidden; running row without entry → prompt visible; settled row (`success`) + leftover `runTool:` entry → prompt visible; `subagent_result` row unchanged.
- Update the WebUI spec — files: [.vorch/specs/webui.md]
  - Extend the sub-agent status-projection bullets: the projection now also carries `runTool:`/`sessionTool:` keys recorded from bridged child `tool_call_started` events (reset on `run_started`), and the spawn row's preview swaps to the child's last tool name while the row is running.

**Done when:**

- New Vitest cases in `chatRunStream.test.js`, `chatTimelinePresentation.test.js`, and `ChatAssistantRun.test.js` pass.
- `python scripts/quality-frontend.py webui/src/lib/chatRunStream.js webui/src/lib/chatTimelinePresentation.js webui/src/components/chat/ChatAssistantRun.svelte webui/src/components/ChatView.svelte` is green (incl. build).
- Manual check: spawn a non-blocking sub-agent; while the child runs, the parent row's preview updates per child tool call (e.g. `read` → `bash`); after completion the row shows the prompt preview again and the result still auto-loads.
- `git diff` touches no backend file.

**Risks / Assumptions:**

- Assumption: the tool name replaces the prompt only while running and reverts at terminal state (it's a liveness indicator). If "keep the last tool visible after completion" is preferred, only the Phase-2 gate changes.
- Assumption: `subagent_result` rows keep their session-id preview; only spawn rows change.
- While `/ws` is reconnecting, tool-name updates pause (status dots already share this property). After the `connection_ready` snapshot, the name reappears with the child's next tool call — the requirement explicitly accepts "shown only once a tool call happened".
- A displayed child session receives events via both SSE and WS; an SSE replay overlapping live WS delivery can transiently rewrite an older tool name, which self-corrects as the replay reaches the newest event. Accepted for a liveness hint.
- Each child tool call now triggers one `subAgentRunStatuses` LRU merge + reactivity pass; see performance note — negligible against the existing 33 ms streaming flush.
