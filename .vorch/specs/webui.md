# WebUI

Svelte accessor that talks only to the vBot server through HTTP RPC, Server-Sent Events, and WebSocket.

## Overview

`webui/` owns the browser interface. It does not import Python/core code and it
does not talk to providers directly. The product presents an Agent-first chat surface, Agent management, a functional Settings view with General, Skills, Defaults, Sub-Agents, Compaction, Providers, and Appearance sub-panels, a functional System Prompt tab, and a functional Logs tab for read-only daily log viewing.

## Layout

- The WebUI uses the Toasted two-pane app shell: fixed 210px navigation on the
  left and content on the right.
- The left navigation contains at least these entries:
  - `Chat`
  - `Agents`
- `Cron`
  - `System Prompt`
  - `Settings`
  - `Logs`

## Interfaces

- `webui/src/lib/api.js`
  - `rpc(method, params?, options?)` posts to `/api/rpc` and returns `result` or
     throws `ApiClientError` with a stable `code`.
  - `uploadAttachment(file, options?)` posts one multipart file to `/api/upload`
    and returns `{ attachment_id, filename, media_type, size_bytes }`.
  - `getAttachmentUrl(attachmentId)` returns `/api/attachments/<id>` for use in
    `<img>` or download links.
  - `listSessions(agentId, options?)` calls `session.list` and returns
    `{ sessions }` for one Agent.
  - `listQueue(agentId, sessionId, options?)`, `removeFromQueue(agentId,
    sessionId, itemId, options?)`, and `updateQueueItem(agentId, sessionId,
    itemId, content, options?)` wrap `chat.queue_list`, `chat.queue_remove`,
    and `chat.queue_update`.
  - `linkSessionToChannel(agentId, sessionId, channelId, platformConvId, options?)`
    calls `session.link_channel`.
  - `listChannels(options?)`, `createChannel(payload, options?)`,
    `updateChannel(channelId, payload, options?)`, `deleteChannel(channelId,
    options?)`, `enableChannel(channelId, options?)`,
    `disableChannel(channelId, options?)`, and `getChannelStatus(channelId,
    options?)` wrap the `channel.*` RPC methods.
  - `listCronJobs`, `createCronJob`, `updateCronJob`, `deleteCronJob`,
    `enableCronJob`, and `disableCronJob` wrap the `cron.*` RPC methods.
  - `listLogs(options?)` calls `log.list` and returns `{ files, default_file }`
    for the daily logs catalog.
  - `readLogFile(file, options?)` calls `log.read` and returns
    `{ file, entries, cursor }` for one selected daily log file.
  - `subscribeRunEvents(sseUrl, handlers, options?)` opens an `EventSource` for
    one Run timeline and returns `{ close, source }`. It subscribes to whole
    Run events plus streaming delta events and supports optional
    `afterSequence` URL construction for manual replay; native reconnect uses
    SSE event IDs / `Last-Event-ID` from the server.
  - `subscribeServerEvents(handlers, options?)` opens `/ws` and returns
     `{ close, socket }`. Supports `afterSequence` option for reconnect replay.
  - `subscribeLogEvents(file, handlers, options?)` opens
    `/ws/logs?file=...&cursor=...` and returns `{ close, socket }` for one
    selected log file. Callers should pass the latest `cursor` from
    `readLogFile(...)`.
- `webui/src/lib/i18n.js`
  - `t(key, fallback?, values?)` is required for all user-visible strings.
- `webui/src/lib/connectionState.js`
  - Manages persistent WebSocket connection state: `createConnectionState()`,
    `connect(state, handlers)`, `disconnect(state)`.
  - Three statuses: `CONNECTION_STATUS_CONNECTED`, `CONNECTION_STATUS_RECONNECTING`,
    `CONNECTION_STATUS_DISCONNECTED`.
  - Reconnect with exponential backoff (1s initial, 30s max, ±25% jitter).
  - Tracks `lastSequence` for `after_sequence` reconnect replay.
- `webui/src/lib/chatState.js`
  - Pure helpers for selected Agent, per-Agent/current-Session state, visible
    timeline items, active Run status, ordered streaming buffers, and FIFO
    queued messages. Visible timeline aggregation groups Run events into one
    `assistant_run` item per Run so thinking, tool lifecycle rows, and assistant
    output render together.
  - Live `model_fallback_activated` Run events are aggregated into the current
    `assistant_run` as a `model_fallback` child item so the switch appears inline
    with the rest of the Run rather than as a standalone chat message.
  - `role: "error"` history messages and live `error_message_persisted` Run events
    render as standalone message timeline items, never inside an assistant Run.
  - `role: "compaction_checkpoint"` history messages and live `compaction_completed`
    Run events become `compaction_separator` timeline items rather than normal
    chat bubbles.
- `webui/src/lib/toastState.js`
  - Pure helpers for app-level toast state: `createToastState()`, `addToast(...)`,
    and `dismissToast(...)`.
- `webui/src/lib/markdown.js`
  - Owns the shared `markdown-it` singleton for assistant-output rendering and
    exports `renderMarkdown(src)` for settled assistant text plus
    `renderMarkdownStreaming(src)` for in-flight assistant text with open-fence
    handling, both with raw HTML disabled plus safe external-link attributes.
- `webui/src/lib/logsView.js`
  - Pure helpers for Logs tab state: initial state, catalog application,
    selected-file changes, append/reset stream merging, level option derivation,
    and local text filtering across timestamp/level/logger/message/continuation.
- `webui/src/lib/sessionListView.js`
  - Pure helpers for session drawer state: `createSessionListState()`,
    `applySessionList(...)`, `selectSession(...)`, and display-name derivation
    for normal and channel-backed sessions.
- `webui/src/lib/channelSettings.js`
  - Pure helpers for Channels settings state and payload building: initial
    state, channel-list normalization, and create/update payload builders.
- `webui/src/lib/cronView.js`
  - Pure helpers for the Cron tab state: initial state, job normalization,
    completed-job filtering, and create/update payload builders.
  - Once-job edit payloads preserve the original stored `run_at` instant when
    the user does not change the scheduled value, so opening and saving a job
    does not shift its fire time.
- `webui/src/components/ToastStack.svelte`
  - Renders dismissable toast notifications from toast state using the shared
    toast CSS classes.
- `webui/src/components/ChatView.svelte`
  - Loads `chat.commands` on mount and passes the flat combined command/skill list to the composer as the existing `availableSkills` prop shape for trigger suggestions.
  - Shows inline neutral `actionInfo` feedback when `chat.stream` handles a built-in command without starting a Run.
  - When a manual `/compact` command is handled without starting a Run, ChatView reloads the active session history so the new compaction separator appears immediately.
  - When the backend rejects `/compact` because the target Session already has an active Run, ChatView surfaces the handled reply inline and does not start a second Run.
  - Sends message submits to the server even while the Session has an active Run. When `chat.stream` returns `{ queued: true, item }`, ChatView adds that server queue item locally and does not open an SSE subscription for it.
  - Refreshes queued-message state from `chat.queue_list` after history load and after terminal Run events so the accessor reflects the server-owned queue after reloads, reconnects, and drain transitions.
  - Owns the session drawer toggle plus local `viewingSessionId` override state.
    Selecting a session from the drawer loads its history without mutating the
    Agent's persisted `current_session_id`.
- `webui/src/components/QueuedMessages.svelte`
  - Renders queued server-backed messages with remove and inline edit controls. Edit mode is local UI state; save persists through `chat.queue_update` and cancel only exits the local editor.
- `webui/src/components/SessionListDrawer.svelte`
  - Renders the per-Agent session list with platform badges, last-active
    metadata, selection callbacks, and inline retroactive channel-linking flows
    backed by `session.link_channel`.
- `webui/src/components/ChatComposer.svelte`
  - Supports `/skill-name` at the start of input and `$skill-name` inline autocomplete. Selection inserts only the trigger token and preserves the rest of the message text exactly; backend chat activation handles loading.
  - Built-in command autocomplete consumes bare command names from `chat.commands` and inserts the `/` prefix exactly once at compose time.
  - Supports attachment uploads via file picker, image paste, and drag-and-drop.
  - Maintains local pending attachments with `preview_url` object URLs and builds
    canonical message `content` as `string` or `list[ContentBlock]` on send.
- `webui/src/components/SkillAutocomplete.svelte`
  - Renders a flat combined command/skill name/description list for composer trigger contexts. Skills with validation warnings are still loadable and may appear; invalid/non-loadable diagnostics are excluded by ChatView data flow.
- `webui/src/lib/agentForm.js`
  - Normalizes Agent create/update form values into RPC payloads. Workspace is
  displayed from Agent data but omitted from public create/update payloads.
  - In edit mode it builds sparse update payloads: unchanged fields are omitted
    so inherited resolved defaults are not written back as explicit overrides.
    Clearing `temperature` or `thinking_effort` sends `null`; clearing model
    fields sends `""`.
- `webui/src/components/AgentsView.svelte`
  - Loads `agent.list` plus the backend catalogs from `model.list`,
    `connection.list`, `tool.list`, and `skill.list` on mount.
  - Calls `model.list` without capability/context filters so all models from
    usable configured providers remain visible. Local OpenAI-compatible
    providers such as future Ollama or LM Studio integrations may expose sparse
    or user-tuned catalog facts, and the Agent selector must not hide them based
    on missing tool/context metadata.
  - The Agent form uses backend-backed selects for `model`, `fallback_model`,
    and `thinking_effort`, plus a tool-toggle list sourced from `tool.list`.
  - The Agent skill section is backend-backed by `skill.list`. Loadable skills are shown with name, description, warning text, and toggles that write array-based `allowed_skills`. Non-loadable skills from `invalid_skills` render in a separate unavailable list with warnings and no toggles.
  - In edit mode, the primary save action lives in a sticky footer at the bottom
    of the detail scroll area; the destructive delete action stays in the top
    detail action cluster.
  - Model and fallback-model selects store both the canonical public model ID and
    selected connection. If a provider has one usable connection, the label stays
    as the model ID (for example `openrouter/anthropic/claude-sonnet-4`). If a
    provider has multiple usable connections, the label adds the connection
    label suffix (for example `openai/gpt-5.4 (OAuth)`).
- `webui/src/lib/settingsView.js`
  - Normalizes Settings provider metadata that now uses credential-centric
    fields (`credential_key`, `credentials_configured`) rather than env/API-key
    wording.
  - Delegates Channels panel state and payload logic to `channelSettings.js` so
    channel form parsing and validation stay centralized.
  - Provides OAuth provider helpers for Settings: OAuth connection detection,
    public connection-id payload construction, and local connection status
    derivation (`connected`, `disconnected`, or `pending`).
  - Settings provider metadata includes `models_endpoint`; when at least one
    provider is refresh-capable and credential-configured, the Providers panel
    shows one global model database refresh control. It calls `model.refresh_db`
    without `provider_id` and then re-requests `model.list` after success.
  - Normalizes skill directory settings from `settings.skills.directories` and builds update payloads for `settings.update`.
  - Normalizes `settings.defaults.agent` for the Defaults panel and builds
    payloads that preserve backend semantics: `null` removes a default,
    `thinking_effort: ""` means explicit provider default, and `null` means no
    global default.
  - Normalizes Sub-Agent settings from `settings.subagents` and builds update payloads for `settings.update`.
  - Normalizes Compaction settings from `settings.compaction` and builds the corresponding `settings.update` payload.
- `webui/src/components/SettingsView.svelte`
  - Includes a Defaults panel. It lets users edit project-wide Agent fallback
    values for `model`, `fallback_model`, `temperature`, and
    `thinking_effort` through `settings.update({ defaults: { agent: ... } })`.
    `temperature` supports explicit clear-to-null; `thinking_effort`
    distinguishes between no default and provider default.
  - Includes a Skills panel. It displays the default data-directory skill path as read-only and lets users add, remove, and save extra `skill_directories` entries through `settings.update`.
  - Includes a Sub-Agents panel. It lets users edit `max_subagent_depth`, `max_subagents_per_turn`, and `subagent_timeout_minutes` through `settings.update`.
  - Includes a Compaction panel. It lets users edit `auto`, `threshold`, `tail_tokens`, and `summary_model` through `settings.update`.
  - The Appearance, Skills, Sub-Agents, and Compaction panels auto-save about
    800 ms after the last dirty edit. Their manual save buttons remain visible
    in sticky footers inside the panel scroll area, stay enabled for trust, save
    immediately when dirty, and show an "Already saved" success toast when the
    panel is already clean.
  - Includes a Channels panel. It lists configured channels, hydrates per-row
    running state via `channel.status`, and supports create, edit, delete,
    enable, and disable actions through the `channel.*` RPC methods.
  - The Providers panel renders OAuth connections with Connect/Disconnect
    controls. `provider.connect` opens an inline Device Flow dialog with the user
    code, copy control, and verification link; `provider_auth_completed` closes
    it and refreshes settings. Settings sends public compositional connection IDs such as
    `github-copilot:oauth` in provider RPC payloads.
- `webui/src/components/ChatTimeline.svelte`
  - Shows day separators only when the visible Session timeline spans more than
    one local calendar day. In multi-day histories, each day group gets its own
    separator and the current local day is labeled with `chat.today` (`Today`).
  - Renders tool-call summary text from
    `tool_call_started.payload.display.summary` when available. Legacy
    argument-label fallbacks exist only for history or old events without a
    display payload; empty summaries render no parenthesized argument text.
  - Renders `subagent` and `subagent_result` tool calls with a Sub-Agent label, target Agent identifier, compact argument preview, status text, and a session navigation link when the tool result includes `agent_id` and `session_id`. When a later `subagent_result` completes the same child session, the original `subagent` spawn row should no longer appear stuck in `running` state.
  - Renders `model_fallback` assistant-run children as a small inline informational notice using i18n text.
  - Renders `compaction_separator` timeline items as a date-separator-style inline notice using the `chat.compacted` i18n label.
  - Renders assistant streaming output through `renderMarkdownStreaming(...)` and settled assistant run output plus persisted assistant messages through `renderMarkdown(...)`, all inside a scoped `.msg-markdown` container so normal agent replies display headings, lists, links, tables, and code fences as Markdown instead of raw source while long open fenced blocks remain inspectable during streaming.
  - Renders user message content arrays block-by-block: `text` as normal message text,
    image `media` via `/api/attachments/<id>`, and `file` as download links.
- `webui/src/components/LogsView.svelte`
  - Loads the daily logs catalog on mount, selects the newest file by default,
    reads one file at a time through `log.read`, applies local level/search/sort
    controls in the accessor, and owns a dedicated `/ws/logs` subscription with
    reconnect and cleanup scoped to the currently selected file.
  - Uses the shared simple dropdown style for file selection, level filtering,
    and newest/oldest order controls.
  - Uses the `cursor` from each `log.read` response when opening the live log
    socket so append events are not lost during the read→subscribe handoff.
- `webui/src/components/CronView.svelte`
  - Loads `cron.list` and `agent.list` on mount, filters completed jobs out of
    the rendered table, exposes create/edit/delete and enable/disable actions,
  and refreshes the job list after every mutation.
  - Edit flows preserve server-provided `session_id` values unless the user
    explicitly changes them.
- `webui/src/components/SystemPromptView.svelte`
  - Renders the editable prompt fragments with reset controls near the fragment
    header, while the primary save button lives in a sticky footer below each
    textarea.
  - Dirty fragments auto-save about 800 ms after the last edit using per-fragment
    debounce timers. Manual save stays enabled, saves immediately when dirty,
    and shows an "Already saved" success toast when the fragment is already clean.
- `webui/src/App.svelte`
  - Owns app shell navigation and shares Agent selection/refresh state between
    Chat and Agents views.
  - Routes the top-level `Cron` navigation item to `CronView` between Agents and
    System Prompt.
  - Routes the top-level `Logs` navigation item to `LogsView`.
  - Handles server-pushed `app_error` WebSocket events as error toasts.
  - Forwards `provider_auth_completed` WebSocket events to `SettingsView` when
    the Settings view is active.

## Conventions

- Svelte code uses JavaScript, not TypeScript.
- Use Svelte 5 callback props for component communication; do not use event
  dispatchers for new components.
- All visible text goes through `t(...)`; add i18n keys with tests when new UI
  copy is introduced.
- Browser resources (`EventSource`, `WebSocket`) must expose explicit cleanup and
  be closed on component destroy.
- The Cron tab is backend-driven through `cron.*` RPC methods and shows only
  active and paused jobs; completed jobs stay hidden from the normal table.
- The Logs tab is read-only and file-backed. It reads one selected daily log file
  at a time and must not depend on chat/session state or the shared app event bus.
- The Logs tab should pass the most recent `log.read` cursor into
  `subscribeLogEvents(...)` whenever it opens or reopens the dedicated log
  socket.
- The UI normally selects Agents, not Sessions. The shown chat defaults to the
  selected Agent's `current_session_id`, but explicit session overrides may be
  opened through the Session drawer or sub-agent session links. These overrides
  are accessor-local UI state only; they do not mutate the kernel's persisted
  `current_session_id`. Sub-agent session overrides stay writable and show a
  footer notice above the composer with an explicit “Return to current session”
  action. Normal new-session/agent-selection actions also clear the override.
- Queue state shown in the accessor is a server-backed projection scoped by
  Agent plus current Session. The accessor may apply optimistic local
  add/remove/update helpers, but `chat.queue_*` is the source of truth and
  queued items survive tab close until the server drains them or restarts.
- Attachments are uploaded over the dedicated HTTP endpoints, not through RPC.
  The outgoing chat payload still uses the canonical `content` field, switching
  from plain string to `list[ContentBlock]` only when attachments are present.
- Streaming output is accessor-local/in-memory. `streamingItems` preserves the
  provider-visible order of reasoning, assistant text, and tool-call deltas;
  the final `assistant_output` event clears the buffer and becomes the
  authoritative rendered message.
- Assistant output Markdown rendering is accessor-side only. It applies to live
  streaming assistant text, completed assistant output, and persisted assistant
  history messages. User text, reasoning bodies, tool details, and error
  messages remain plain pre-wrapped text.
- Visible chat rendering treats an assistant Run as one assistant block. Tool
  lifecycle events (`tool_call_started` and `tool_call_result`) are merged into a
  single expandable tool row inside that block rather than rendered as separate
  chat messages.
- Visible chat history accepts normal user, assistant, tool, and persisted error
  messages plus persisted `compaction_checkpoint` records; kernel-internal
  note/system-reminder entries must be filtered out if they ever arrive from a
  server response.
- Persisted error messages are visible chat timeline items with an error label and
  distinct red treatment.
- Persisted `compaction_checkpoint` records are visible timeline markers only;
  they must not render as normal message bubbles.
- Skill trigger autocomplete is a composer aid only. It must not fetch or inject skill content directly; `/skill-name` and `$skill-name` text is sent unchanged for backend deterministic activation.
- Built-in command handling feedback is accessor-local UI state only. When the backend returns `{ command_handled: true, reply }`, ChatView shows the reply inline and must not subscribe to a Run stream for that submit.
- Partial tool-call argument JSON may be accumulated internally but must not be
  displayed as final normal UI data before the complete `tool_call_started`
  event arrives.
- In Chat, only the message timeline should scroll. The agent bar, notices,
  queued-message region, and composer stay visible inside the bounded view.

## Constraints & Gotchas

- The Toasted `Components` showcase is a design/reference artifact only. It
  must not ship as a live WebUI tab or appear in normal navigation.
- Reload recovery for in-flight Runs is out of scope. The accessor-local
  last-selected Agent is restored through `localStorage` when available.
- `New Session` is blocked while the selected Agent/current Session has an active
  Run. Switching to another Agent while a Run is active is allowed.
- `System Prompt` is functional — it renders five fragment editors (`system.md`, `runtime.md`, `tools.md`, `channels.md`, `skills.md`) with save/reset/variable-reference, plus a preview section with agent picker, refresh, copy, and token count. `Settings` is functional and contains the General (server host, data directory), Skills (default skill path and extra scan directories), Defaults (project-wide Agent fallback values), Sub-Agents, Compaction, Providers (credential status, model counts, model database refresh), and Appearance (language preference) sub-panels. In the Agents view, model, tool, and skill catalogs are backend-backed.
- `Logs` is functional — it shows one selected daily log file, defaults to the
  newest file, keeps the current selection sticky when newer files appear, and
  applies level filtering, newest/oldest local ordering, and free-text search
  locally in the accessor.
- The production build emits `webui/dist`, which FastAPI serves when present.
