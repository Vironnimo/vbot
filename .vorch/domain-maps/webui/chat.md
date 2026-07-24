# WebUI Chat

Read this reference only for WebUI Chat selection, Session, history, Run, Queue, timeline, composer, attachment, command, or speech-input work. Backend Chat contracts live in `chat.md` and its task-gated references.

## Ownership and addressing

`ChatView.svelte` composes Chat presentation and applies navigation consequences, while `createChatController()` in `chatState.js` owns the reactive client projection plus roster/history/command loading, send and Queue workflows, cancellation, and durable-state reconciliation. `chatRunStream.js` owns one active Run stream, and `chatTimeline.js` derives renderable timeline items. Presentation components consume those projections and forward user intents; they do not sequence Chat RPCs or rebuild Run state independently.

An Agent address can be global or Project-scoped. `resolveAgentAddressing()` and `formatAgentAddress()` are the canonical client helpers for that distinction. Chat's selected Project context changes which Agent address and Session collection are active; it does not change the Project selected for management in `ProjectsView`.

## Selection and Sessions

- Display state is override-first: a temporary UI choice can lead while a server mutation is pending, but the resulting Agent, Project, and Session state must reconcile to server responses.
- Session state is keyed by Agent address and Session id so switching Agents or Project context does not merge histories, Queue items, usage, or active Runs.
- Agent navigation projects status across all held Sessions with strict priority `running` → `unread` → `idle`: running is amber/orange, a durable unread terminal result is blue, and idle is neutral. Selection remains a separate accent-text/underline state. `connection_ready.active_runs` supplies activity after reconnect, while `session.list` supplies durable unread state; clicking a blue Agent opens its newest unread Session. A Run carrying `contributes_to_agent_activity: false` is excluded from this projection and from active Subagent status maps even though its Session-local state remains running/terminal for direct viewing.
- A completion is marked read only after the exact `run_id` appears in the displayed Session's history or terminal Run events. The displayed Session is excluded immediately from Agent and Session-drawer unread presentation while its Run-specific `session.mark_read` acknowledgement is pending; other Sessions remain eligible, and a failed acknowledgement becomes visible again after leaving the Session. Successful acknowledgements performed internally after a Sub-Agent result reaches its Parent arrive through the same Sessions invalidation and listing refresh as Accessor reads. `session.list.latest_completion_run_id` is retained after a read and is the exact reconciliation key for retained/replayed terminal App-shell events: either arrival order must stay read, while a clean listing for an older completion must not clear a newer local result.
- History initially loads the newest page and prepends older pages without changing the user's visible reading position. Persisted history and live Run events are reconciled by identity/sequence rather than appended blindly.
- A history response may always update its addressed Session state, but only the response for the still-displayed Session may change global loading/error state or attach the Run stream. This stale-display guard belongs to `createChatController()`, not the View.
- Creating, renaming, deleting, moving, or selecting a Session uses backend responses as the final identity and routing decision. Project-Agent addressing and move targets are normalized by the helpers in `chatState.js`; because a Project Agent has no server `current_session_id`, its initial Agent-bar landing selects the newest non-Sub-Agent Session and creates a fresh Session when only execution-owned Sub-Agent Sessions exist.
- Selecting another same-Agent Session is ordinary browser navigation: the selected row and loaded history identify what the browser is using. The identity Agent's backend `current_session_id` remains a default landing pointer, not an age or usability classification, so the WebUI does not label another selected Session "past," show a return warning, or expose that pointer as a competing "Current" badge. Cross-owner Sub-Agent navigation still shows its contextual return banner.
- New-Session availability is constrained by active local Run state; a stale local Run can be reset only through the explicit reconciliation path.

## Runs, streaming, and recovery

- `createChatRunStream()` owns the Run EventSource lifecycle, expected sequence, reconnect cursor, heartbeat watchdog, and terminal cleanup for one Run. Replacing or leaving that Run closes the old stream and all pending reconnect/recovery timers.
- `chatState.js` accepts ordered Run events and tracks the highest contiguous sequence. Gaps trigger recovery/reconnect behavior; duplicates and already-persisted events must not produce duplicate timeline items.
- WebSocket lifecycle summaries can reveal a Run that started or finished elsewhere. They cause reconciliation, while the full output stream still comes from SSE.
- The `connection_ready.active_runs` snapshot replaces locally projected active Run and Subagent status. Every active Run creates/refreshes its addressed Session state, but only contributing Runs project running Agent/Subagent status; excluded Runs still reattach SSE when their Session is displayed so direct inspection remains live. If the displayed local Run is absent, the client reconciles through `chat.history` before clearing it so a missed terminal event reveals the durable final answer; an active durable Run is reattached, and transient History failures retry without executing the Run again. Active `run:` and `session:` Subagent entries absent from the snapshot are removed, while terminal status, duration, tool, and Queue-to-Run metadata remain.
- Terminal events complete, fail, or cancel the local Run projection and close its stream. Interruption checkpoints are internal Chat state: history and terminal events do not project them, the UI shows no recovery banner or controls, and the next normal visible user message receives the checkpoint inside the backend.
- Cancelling a Run and cancelling a cancellable tool call are distinct operations. The UI must preserve that distinction in labels, availability, and error handling.
- `sendMessage()` returns a semantic outcome (`move`, `switch`, `transient`, `toast`, `queued`, `started`, or failure) after it has reconciled Session/Run/Queue state. `ChatView` applies only the navigation or presentation consequence of that outcome.

## Timeline and Queue

- `visibleTimelineItemsForRender()` is the render boundary. `ChatTimeline.svelte` and its item components display derived items; they do not reconstruct event ordering from raw arrays.
- Persisted messages prune matching transient Run events. Assistant output, reasoning, tool activity, child-Agent progress, compaction checkpoints, usage, and errors retain their distinct timeline semantics. A text/reasoning child may remain a transient draft for later stable-event reconciliation, but only the latest visible streaming child of a running Run may display the working caret; a later Tool or text child proves earlier content is no longer the current activity.
- The special Sub-Agent timeline row belongs only to the `subagent` spawn because it owns Child Session navigation, progress, and cancellation. `subagent_result` is rendered through the ordinary Tool Call row with its normal name, Args, Result, and timing; its Sub-Agent-shaped payload does not grant it a View Session link or Child Run cancel action. When a completed background spawn row fetches durable Child history for display, it selects the exact Run segment by `run_id` and requires its terminal Run Summary instead of treating the Session's latest Assistant turn as final.
- Chat copy actions preserve semantic boundaries rather than copying rendered DOM: one Assistant action joins only that Run's ordered `assistant_output` Markdown sections, user copy reconstructs text plus `@` file mentions while omitting binary attachments, reasoning and command output copy independently, and Tool detail actions copy the same sanitized compact value the row displays. `MarkdownContent.svelte` is the shared safe-Markdown presentation owner; it mounts the shared `CopyButton` into renderer-owned fenced-code headers and withholds code copy for an incomplete streaming fence.
- The server owns Queue order and contents. The client may show optimistic continuity for edit/remove operations, but `syncQueueFromServer()` is the authoritative reconciliation path.
- `connection_ready.queues` authoritatively replaces every held Session's public Queue projection, including clearing scopes omitted from the complete snapshot. On `epoch_changed`, locally shown item ids absent from the new process trigger a transient restart-loss notice; a same-process replay gap reconciles silently because missing items may have started normally while disconnected.
- Queue invalidation applies only to the addressed Agent and Session. Switching Session must not display another Session's queued items.
- The timeline is Chat's scrolling surface. Autoscroll follows the existing near-bottom/user-intent rules; the page and composer do not become competing scroll containers.

## Composer and inputs

- `ChatComposer.svelte` owns draft entry and delegates stateful parsing or media behavior to the established helpers. Sending plain text, a slash command, attachments, file mentions, or speech-derived text must use the same resolved Agent/Project/Session address.
- Current-session setup guidance is prerequisite-ordered: `App.svelte` passes Chat the tri-state, Settings-backed usable-Provider state; an explicitly missing Provider routes to Settings → Providers and takes priority over Model assignment, while a connected Provider plus a model-less Identity Agent routes to Agents. Chat must not infer Provider readiness from the Model catalog, and it shows neither prompt while Settings are unresolved.
- Slash commands are discovered from the server and submitted through the Chat command contract; the frontend must not maintain a competing command roster or interpret backend-only command semantics.
- File mentions are constrained to the server-provided file list for the active address. Attachment ids are opaque public identifiers; previews and downloads use safe server URLs rather than filesystem paths.
- Speech input owns browser media resources only for its capture lifetime and releases streams, recorders, timers, and object URLs on completion, cancellation, replacement, or teardown.
- Drafts and picker state are interaction state, not Session history. A refresh or selection transition must not silently submit or cross-address a draft.

## Usage and errors

Usage displayed in Chat is a server-produced Session projection. The frontend formats values and updates the current Session state, but it does not recalculate Provider cost or infer missing usage from rendered text.

Errors are normalized at the transport boundary, then attached to the relevant Session, Run, Queue operation, or transient toast. Session action errors and Run-stream recovery warnings live on their addressed Session; command-catalog errors use a separate latest-request-wins projection. History/send admission failures must not be promoted into Run failures. Rendering a failure must not discard recoverable history or leave a finished Run marked active.

## Source and tests

- State and addressing: `webui/src/lib/chatState.js`
- Run transport: `webui/src/lib/chatRunStream.js`, `webui/src/lib/api.js`
- Timeline projection: `webui/src/lib/chatTimeline.js`
- Composition and input: `webui/src/components/ChatView.svelte`, `ChatTimeline.svelte`, `ChatComposer.svelte`, `webui/src/components/chat/MarkdownContent.svelte`, and the other presentation components under `webui/src/components/chat/`
- Focused coverage: the split `webui/src/lib/__tests__/chatState.test.*.test.js` suites, `chatRunStream.test.js`, `chatTimeline.test.js`, `chatTimelinePresentation.test.js`, `composerMemory.test.js`, and Chat component tests under `webui/src/components/__tests__/`
