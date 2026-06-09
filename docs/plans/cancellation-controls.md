## Plan: Granular cancellation — subagents & bash calls

**Goal:** The user can cancel individual sub-agents and individual bash calls from the UI while they run, without cancelling the whole parent Run; cancelling a parent Run cascades to everything in the session except background (non-blocking) sub-agents, which keep running; and whenever the user cancels a sub-agent or bash call the owning agent is explicitly told it was "cancelled by user".

**Context:** Today cancellation is Run-scoped only (`chat.cancel(run_id)`). A parent cancel already cascades to foreground tools, background bash processes (`process_manager.cancel_scope(run.id)`), and *all* sub-agents recursively (`_attach_parent_cancellation`). There is no per-tool-call or per-sub-agent cancel, background sub-agents are wrongly cascade-cancelled, and the agent is never told a cancellation was user-initiated. The user wants finer control: keep background sub-agents alive on parent cancel, add per-item cancel buttons, and surface a "cancelled by user" signal back to the model.

**Requirements (from the user, verbatim intent):**
- Parent-agent cancel must cancel everything else in that session: running tool calls, background tool calls, sub-agents, their sub-agents, and their tool calls.
- **Exception:** background (non-blocking) sub-agents should keep running on parent cancel. This policy must be **easy to flip back** later — single, clearly-located decision point.
- Every sub-agent (background or not) gets a cancel button while it runs.
- Every bash call gets a cancel button while it runs — abort the bash without aborting the Run.
- The cancel mechanism is generic, but buttons are shown **only** for sub-agents and bash for now.
- Show the cancel button with a ~0.5s delay so fast bash calls (the majority) don't flicker a button.
- Whenever the user cancels a sub-agent or bash call, the agent must be told "cancelled by user" (or similar).
- For non-blocking sub-agents the user-cancel signal rides the existing batch-completion note — but if the agent fetches results early via `subagent_result`, the user-cancel status must already be reflected there.

**Scope:**
- In: per-tool-call cancellation on `Run`; a cancel "reason" that reaches the cancelled result/note; `ToolContext` cancel-registration + user-cancel-check hooks; bash returns a `cancelled_by_user` envelope; sub-agent cascade policy split (blocking cascades, non-blocking does not); `subagent_result` + batch note reflect user-cancel; new RPC(s); WebUI cancel buttons for bash + sub-agent rows with delayed reveal; spec + i18n updates; tests.
- Out: cancel buttons for non-bash, non-sub-agent tools (mechanism stays generic so they can be added later); cancelling queued *user* chat messages (already covered by `chat.queue_remove`); restart/resume of a cancelled item; persistence of cancellation across process restart.

**Assumptions & Constraints:**
- A per-tool-call cancel must **not** set `run.cancel_requested` — the Run continues and the agentic loop proceeds with the cancelled tool result.
- Sub-agents are themselves Runs, so a running sub-agent is cancelled through the same `ChatRunManager` Run machinery (reused, not duplicated).
- Cancellation stays best-effort and in-memory (consistent with `runs.md`).
- No legacy compatibility branches (project rule).

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Cancel primitives | `Run` supports a cancel reason and a per-tool-call cancel registry; unit-tested. |
| M2 | Tool plumbing | `ToolContext` exposes on-cancel registration + user-cancel check; bash kills its process and returns `cancelled_by_user`; dispatch wires it. |
| M3 | Sub-agent policy | Non-blocking sub-agents survive parent cancel; user-cancel reason flows into blocking result, `subagent_result`, and batch note. |
| M4 | RPC | Server exposes per-tool-call cancel and a reason-carrying sub-agent/run cancel. |
| M5 | WebUI | Delayed cancel buttons on bash + sub-agent rows, wired to RPC, i18n + tests. |

### Phase Breakdown

#### Phase 1: Cancel primitives on `Run`
**Goal of this phase:** `Run` can be cancelled *with a reason*, and individual in-flight tool calls can be cancelled without cancelling the Run.
**Can run in parallel with:** none (foundation).

- Add an optional cancel reason to `Run.request_cancel(reason: str | None = None)`, store `self.cancel_reason`, and include it in the `run_cancelled` terminal payload (`mark_cancelled`). Default behavior unchanged when no reason is passed. — read: [.vorch/specs/runs.md], files: [core/runs/runs.py]
- Add a per-tool-call cancel registry to `Run`: `register_tool_cancel(tool_call_id, callback)`, `cancel_tool_call(tool_call_id) -> bool` (invokes callbacks via the existing `_schedule_callback`, marks that call user-cancelled), `tool_call_cancelled(tool_call_id) -> bool`, and `clear_tool_cancel(tool_call_id)`. Cancelling a tool call must **not** touch `cancel_requested` or the executor task. — files: [core/runs/runs.py]
- Export any new public symbols through `core/runs/__init__.py` if needed. — files: [core/runs/__init__.py]
- Tests: reason stored + surfaced in terminal payload; `cancel_tool_call` fires registered callback and flips `tool_call_cancelled`, while `cancel_requested` stays False; unknown tool_call_id returns False; clear removes state. — files: [tests/core/runs/test_runs.py]

**Dependencies:** none.
**Done when:** new `Run` methods are covered by passing tests in `tests/core/runs/test_runs.py`; `python scripts/quality.py core/runs/ tests/core/runs/` is green.

#### Phase 2: Tool-call cancel plumbing (ToolContext, dispatch, bash)
**Goal of this phase:** A running bash call can be cancelled individually; its process is killed and it returns a `cancelled_by_user` envelope that the agent sees.
**Can run in parallel with:** Phase 3 (disjoint files).

- Task A — `ToolContext`/`ToolExecutionConfig` hooks: add `cancel_registration_hook` (registers a cancel callback for *this* call) and `cancel_check_hook` (reports whether *this* call was user-cancelled); expose `ToolContext.on_cancel(callback)` and `ToolContext.was_cancelled_by_user()`. Mirror the existing `emit_hook`/`cancellation_hook` wiring in `_execute_one`. — read: [.vorch/specs/tools.md], files: [core/tools/tools.py]
- Task A — tests for the new hooks (registration invoked, user-cancel check reflects state). — files: [tests/core/tools/test_tools.py]
- Task B ⚡ *parallel with Task C* — bash: after `process_manager.spawn(...)` returns the session id, register `context.on_cancel(lambda: process_manager.kill(session_id, context.agent_id))`; after the foreground phase, if `context.was_cancelled_by_user()`, return `tool_failure("cancelled_by_user", "Command aborted by the user")` instead of the completion/timeout result. For background bash, make the completion watcher report "aborted by the user" wording when the session was user-killed. — read: [.vorch/specs/tools/bash.md], files: [core/tools/bash.py]
- Task B ⚡ — bash tests: user-cancel during foreground returns `cancelled_by_user` and the process is killed; normal completion unaffected. — files: [tests/core/tools/test_bash.py]
- Task C ⚡ *parallel with Task B* — dispatch wiring: in `_dispatch_tool_calls`, pass `cancel_registration_hook=lambda cb: run.register_tool_cancel(<tool_call_id>, cb)` and `cancel_check_hook=lambda: run.tool_call_cancelled(<tool_call_id>)` through `ToolExecutionConfig`; in `_EmittingToolRegistry.dispatch` clear the per-call registry entry in a `finally`. Tool call ids are per-call, so the hook must bind the id at `ToolContext` construction (extend `ToolExecutionConfig` to carry the registrar and let `_execute_one` close over `tool_call.id`). — read: [.vorch/specs/chat.md], files: [core/chat/tool_dispatch.py]
- Task C ⚡ — dispatch tests: a cancelled tool call yields the handler's `cancelled_by_user` envelope, the Run is not cancelled, and the registry entry is cleared afterward. — files: [tests/core/chat/test_tool_dispatch.py]
- Update specs touched: `tools.md` (ToolContext cancel hooks), `tools/bash.md` (per-call cancel + cancelled_by_user envelope). — files: [.vorch/specs/tools.md, .vorch/specs/tools/bash.md]

**Dependencies:** Phase 1 (`register_tool_cancel`, `tool_call_cancelled`). Task B and Task C both depend on Task A.
**Done when:** `python scripts/quality.py core/tools/ core/chat/tool_dispatch.py tests/core/tools/ tests/core/chat/` is green; cancelling a foreground bash mid-run returns `cancelled_by_user` and leaves the Run running.

#### Phase 3: Sub-agent cascade policy + user-cancel signalling
**Goal of this phase:** Background sub-agents survive a parent cancel; cancelling a sub-agent (blocking, non-blocking, or queued) surfaces "cancelled by user" to the parent through the right channel.
**Can run in parallel with:** Phase 2 (disjoint files).

- Gate the parent→child cascade by blocking-ness: only register `_attach_parent_cancellation` for **blocking** spawns (and queued-then-started blocking waits). For non-blocking spawns, do **not** register the cascade, so a parent cancel leaves them running. Keep this the single decision point and comment it as the flip-back switch. — read: [.vorch/specs/subagents.md], files: [core/subagents/subagents.py]
- Thread the cancel reason: `_wait_for_subagent_result(run)` builds the cancelled result from `run.cancel_reason` ("Cancelled by the user" when reason is user) so the blocking `subagent` tool result, the `subagent_result` lookup, and the batch-completion note all show the user-cancel status. — files: [core/subagents/subagents.py]
- Batch note wording: `_entry_status` / `_entry_result_text` reflect a user-cancel entry so the batch note reads "cancelled by user". — files: [core/subagents/tracker.py]
- Queued sub-agent cancel: ensure removing a queued child (via queue removal path) cleans the batch tracker and, if a parent-visible record is expected, marks it cancelled-by-user. Confirm `_track_queued_subagent_completion` already drops it; add reason wording only where a result is produced. — files: [core/subagents/subagents.py, core/subagents/tracker.py]
- Tests: parent cancel does NOT cancel a non-blocking child but DOES cancel a blocking child (and its descendants); a user-cancelled child yields "cancelled by user" in (a) blocking tool result, (b) `subagent_result`, (c) batch note. — files: [tests/core/tools/test_subagent.py, tests/core/subagents/test_tracker.py]
- Update spec: `subagents.md` (cascade-policy split, user-cancel reason in results/note). — files: [.vorch/specs/subagents.md]

**Dependencies:** Phase 1 (`cancel_reason`).
**Done when:** `python scripts/quality.py core/subagents/ tests/core/tools/test_subagent.py tests/core/subagents/` is green; a parent cancel test leaves a non-blocking child running.

#### Phase 4: RPC surface
**Goal of this phase:** Accessors can cancel a single bash tool call and a single sub-agent (running or queued) with a user reason.
**Can run in parallel with:** none (depends on core).

- Add `chat.cancel_tool_call` (`{agent_id?, run_id, tool_call_id}`): look up the Run via `ChatRunManager.get(run_id)`, call `run.cancel_tool_call(tool_call_id)`, map unknown run/tool-call to the existing not-found errors, return `{ok: true}` / not-found. — read: [.vorch/specs/server.md], files: [server/rpc/chat_methods.py]
- Extend sub-agent/run cancel to carry the reason: add an optional `reason` to `chat.cancel` (default None; `"user"` from the UI) so `state.chat_runs.cancel(run_id, reason)` sets the child run's reason. The UI cancels a running sub-agent by its child `run_id`; a queued sub-agent is cancelled through the existing `chat.queue_remove` using the child agent/session/item ids. Confirm `ChatRunManager.cancel` forwards the reason. — files: [server/rpc/chat_methods.py, core/runs/runs.py]
- Validation: accept the new params, reject unsupported fields (match existing `_required_string`/field-allowlist style). — files: [server/rpc/chat_methods.py, server/rpc/validation.py]
- Tests: `chat.cancel_tool_call` cancels a running tool call and returns ok / not-found; `chat.cancel` with `reason="user"` sets the run reason. — files: [tests/server/test_chat_methods.py]
- Update spec: `server.md` (new method + reason param). — files: [.vorch/specs/server.md]

**Dependencies:** Phases 1–3.
**Done when:** `python scripts/quality.py server/rpc/ tests/server/` is green; both cancel paths reachable over RPC.

#### Phase 5: WebUI cancel buttons
**Goal of this phase:** A cancel button appears (after ~0.5s) on each running bash and sub-agent row and calls the right RPC; cancelled items render their cancelled state.
**Can run in parallel with:** none (depends on RPC).

- API client: add `cancelToolCall({agentId, runId, toolCallId})` and a reason-carrying sub-agent/run cancel wrapper (`cancelRun(runId, {reason})`) plus queued-removal reuse. — read: [.vorch/specs/webui.md], files: [webui/src/lib/api.js]
- `ChatAssistantRun.svelte`: render a cancel button in the generic tool-event line when the tool is `bash` and `toolStatus(child) === 'running'`, and in the sub-agent line when `dotStatus === 'running'`. Bash button → `cancelToolCall` with `item.runId` + `child.toolCallId`; sub-agent button → cancel child `subAgentSession.run_id` (or queue removal when only `queue_item_id` exists). Wire via Svelte 5 callback props passed down from ChatView. — files: [webui/src/components/chat/ChatAssistantRun.svelte]
- Pass the cancel callbacks from `ChatView.svelte` (it owns the agent/session/run context and api calls). — files: [webui/src/components/chat/ChatView.svelte]
- Delayed reveal: cancel button starts `opacity:0` and fades in via CSS `animation`/`transition` with a ~0.5s delay so fast tool calls never flash it (no per-call JS timers). — files: [webui/src/components/chat/ChatAssistantRun.svelte]
- i18n: add strings (e.g. `chat.cancelToolCall` "Cancel", `chat.cancelSubAgent` "Cancel", aria labels) with English fallback. — files: [webui/src/lib/i18n.js]
- Helper for "is this tool cancellable" (bash + sub-agent) kept in presentation lib so the rule is testable and extensible. — files: [webui/src/lib/chatTimelinePresentation.js]
- Tests: presentation helper marks bash/sub-agent rows cancellable only while running; component test that the button calls the callback with the right ids and is hidden when not running. — files: [webui/src/lib/__tests__/chatTimelinePresentation.test.js, webui/src/components/chat/__tests__/ChatAssistantRun.test.js]
- Update spec: `webui.md` (cancel controls on tool/sub-agent rows, delayed reveal). — files: [.vorch/specs/webui.md]

**Dependencies:** Phase 4.
**Done when:** `python scripts/quality-frontend.py webui/src/components/chat/ webui/src/lib/` is green; manual check (verify skill) shows a button on a slow bash + a running sub-agent that cancels them and the agent receives a "cancelled by user" result/note.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Per-tool-call cancel accidentally cancels the whole Run | Med | High | Keep `cancel_tool_call` strictly separate from `request_cancel`; explicit test asserts `cancel_requested` stays False. |
| Race: tool finishes just as user clicks cancel | High | Low | `cancel_tool_call` returns False for unknown/cleared ids; UI tolerates a no-op; registry cleared in dispatch `finally`. |
| Non-blocking child's "cancelled by user" never reaches the agent if batch never completes | Med | Med | Reason is also surfaced by `subagent_result` (early fetch) and the batch note fires when remaining entries finish — matches existing completion semantics (accepted by user). |
| Killed background bash reported as generic completion, not user-cancel | Med | Low | Watcher checks user-killed state and uses "aborted by the user" wording. |
| Cancel button flicker on fast bash | High | Low | CSS-delayed fade-in (~0.5s), no DOM removal logic needed. |
| Cascade-policy hard to flip later | Low | Med | Single gated call site for non-blocking cascade, commented as the switch. |

**Open decision (resolved, flag for review):** RPC shape — reuse `chat.cancel` (+`reason`) for running sub-agents rather than a dedicated `subagent.cancel`, since a sub-agent *is* a Run. Default chosen for minimal surface; alternative is a dedicated method if sub-agent cancel later needs different semantics (e.g. queued handling diverging further).
