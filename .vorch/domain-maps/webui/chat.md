# WebUI Chat

Read this reference only for WebUI Chat selection, Session, history, Run, Queue, timeline, composer, attachment, command, or speech-input work. Backend Chat contracts live in `chat.md` and its task-gated references.

## Ownership and addressing

`ChatView.svelte` composes Chat, while `chatState.js` owns the client projection and selection rules, `chatRunStream.js` owns one active Run stream, and `chatTimeline.js` derives renderable timeline items. Presentation components consume those projections; they do not call raw RPC methods or rebuild Run state independently.

An Agent address can be global or Project-scoped. `resolveAgentAddressing()` and `formatAgentAddress()` are the canonical client helpers for that distinction. Chat's selected Project context changes which Agent address and Session collection are active; it does not change the Project selected for management in `ProjectsView`.

## Selection and Sessions

- Display state is override-first: a temporary UI choice can lead while a server mutation is pending, but the resulting Agent, Project, and Session state must reconcile to server responses.
- Session state is keyed by Agent address and Session id so switching Agents or Project context does not merge histories, Queue items, usage, or active Runs.
- History initially loads the newest page and prepends older pages without changing the user's visible reading position. Persisted history and live Run events are reconciled by identity/sequence rather than appended blindly.
- Creating, renaming, deleting, moving, or selecting a Session uses backend responses as the final identity and routing decision. Project-Agent addressing and move targets are normalized by the helpers in `chatState.js`.
- New-Session availability is constrained by active local Run state; a stale local Run can be reset only through the explicit reconciliation path.

## Runs, streaming, and recovery

- `createChatRunStream()` owns the Run EventSource lifecycle, expected sequence, reconnect cursor, and terminal cleanup for one Run. Replacing or leaving that Run closes the old stream.
- `chatState.js` accepts ordered Run events and tracks the highest contiguous sequence. Gaps trigger recovery/reconnect behavior; duplicates and already-persisted events must not produce duplicate timeline items.
- WebSocket lifecycle summaries can reveal a Run that started or finished elsewhere. They cause reconciliation, while the full output stream still comes from SSE.
- The `connection_ready.active_runs` snapshot replaces locally projected active Run and Subagent status. Active `run:` and `session:` Subagent entries absent from the snapshot are removed, while terminal status, duration, tool, and Queue-to-Run metadata remain; visible historical rows can then verify their durable terminal state through `chat.history`.
- Terminal events complete, fail, or cancel the local Run projection and close its stream. A continuation offer is server state and is only shown, continued, or discarded through the corresponding backend contracts.
- Cancelling a Run and cancelling a cancellable tool call are distinct operations. The UI must preserve that distinction in labels, availability, and error handling.

## Timeline and Queue

- `visibleTimelineItemsForRender()` is the render boundary. `ChatTimeline.svelte` and its item components display derived items; they do not reconstruct event ordering from raw arrays.
- Persisted messages prune matching transient Run events. Assistant output, reasoning, tool activity, child-Agent progress, compaction checkpoints, usage, and errors retain their distinct timeline semantics.
- The server owns Queue order and contents. The client may show optimistic continuity for edit/remove operations, but `syncQueueFromServer()` is the authoritative reconciliation path.
- `connection_ready.queues` authoritatively replaces every held Session's public Queue projection, including clearing scopes omitted from the complete snapshot. On `epoch_changed`, locally shown item ids absent from the new process trigger a transient restart-loss notice; a same-process replay gap reconciles silently because missing items may have started normally while disconnected.
- Queue invalidation applies only to the addressed Agent and Session. Switching Session must not display another Session's queued items.
- The timeline is Chat's scrolling surface. Autoscroll follows the existing near-bottom/user-intent rules; the page and composer do not become competing scroll containers.

## Composer and inputs

- `ChatComposer.svelte` owns draft entry and delegates stateful parsing or media behavior to the established helpers. Sending plain text, a slash command, attachments, file mentions, or speech-derived text must use the same resolved Agent/Project/Session address.
- Slash commands are discovered from the server and submitted through the Chat command contract; the frontend must not maintain a competing command roster or interpret backend-only command semantics.
- File mentions are constrained to the server-provided file list for the active address. Attachment ids are opaque public identifiers; previews and downloads use safe server URLs rather than filesystem paths.
- Speech input owns browser media resources only for its capture lifetime and releases streams, recorders, timers, and object URLs on completion, cancellation, replacement, or teardown.
- Drafts and picker state are interaction state, not Session history. A refresh or selection transition must not silently submit or cross-address a draft.

## Usage and errors

Usage displayed in Chat is a server-produced Session projection. The frontend formats values and updates the current Session state, but it does not recalculate Provider cost or infer missing usage from rendered text.

Errors are normalized at the transport boundary, then attached to the relevant Session, Run, Queue operation, or transient toast. Rendering a failure must not discard recoverable history or leave a finished Run marked active.

## Source and tests

- State and addressing: `webui/src/lib/chatState.js`
- Run transport: `webui/src/lib/chatRunStream.js`, `webui/src/lib/api.js`
- Timeline projection: `webui/src/lib/chatTimeline.js`
- Composition and input: `webui/src/components/ChatView.svelte`, `ChatTimeline.svelte`, `ChatComposer.svelte`, and `webui/src/components/chat/`
- Focused coverage: the split `webui/src/lib/__tests__/chatState.test.*.test.js` suites, `chatRunStream.test.js`, `chatTimeline.test.js`, `chatTimelinePresentation.test.js`, `composerMemory.test.js`, and Chat component tests under `webui/src/components/__tests__/`
