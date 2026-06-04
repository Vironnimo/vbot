# WebUI

Svelte accessor that talks only to the vBot server through HTTP RPC, Server-Sent Events, and WebSocket.

## Overview

`webui/` owns the browser interface. It does not import Python/core code and it
does not talk to providers directly. The product presents an Agent-first chat surface, Agent management, a functional Settings view with General, Skills, Defaults, Sub-Agents, Compaction, Recall, Specialized Models, Providers, Debug, Voice (Desktop-only), Channels, and Appearance sub-panels, a functional System Prompt tab, a functional Logs tab for read-only daily log viewing, and a conditional Debug tab for provider wire trace inspection.

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
  - `Debug` (only visible when `debug.enabled` is true)

## Interfaces

- `webui/src/lib/api.js`
  - `rpc(method, params?, options?)` posts to `/api/rpc` and returns `result` or
     throws `ApiClientError` with a stable `code`.
  - `uploadAttachment(file, options?)` posts one multipart file to `/api/upload`
    and returns `{ attachment_id, filename, media_type, size_bytes }`.
  - `getAttachmentUrl(attachmentId)` returns `/api/attachments/<id>` for use in
    `<img>` or download links.
  - `getTaskModelSettings`, `updateTaskModelSettings`,
    `listTaskModelTargets`, and `getTaskModelOptions` wrap the `task_model.*`
    RPC methods.
  - `transcribeSpeech(blob, options?)` posts multipart audio to
    `/api/speech/transcribe` and returns normalized transcription JSON.
  - `synthesizeSpeech(text, options?)` posts JSON to `/api/speech/synthesize`
    and returns a `Blob` containing generated audio.
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
  - `debugStatus()`, `debugTraceList()`, `debugTraceGet(traceId)`,
    `debugTraceClear()`, and `debugModelProbe(providerId, connectionId)` wrap
    the `debug.*` RPC methods.
  - `subscribeRunEvents(sseUrl, handlers, options?)` opens an `EventSource` for
    one Run timeline and returns `{ close, source }`. It subscribes to whole
    Run events, including `subagent_session_started`, plus streaming delta
    events and supports optional
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
  - Live terminal Run events and persisted `role: "run_summary"` messages merge
    `timing.duration_ms` into the corresponding `assistant_run` item. Live
    `tool_call_result.payload.timing` and persisted tool-message `timing` merge
    into the matching tool row, so reload history and live streaming render the
    same durations.
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
    and `dismissToast(...)`. `addToast(...)` returns the generated toast id so
    the app shell can schedule and cancel auto-dismiss timers.
- `webui/src/lib/modelSelection.js`
  - Pure helpers for backend-backed model picker options shared by Agents and
    Settings. They combine `model.list` models with usable `connection.list`
    connections, preserve unavailable/custom saved values, and store selected
    connection-qualified values such as `openai/gpt-5.2::api-key` when the user
    picks a concrete provider connection.
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
- `webui/src/lib/taskModelSettings.js`
  - Normalizes task-model bindings, target lists, and option schemas for the
    Settings Specialized Models panel.
  - Builds sparse update payloads and compares normalized bindings for dirty
    checks.
- `webui/src/lib/audioRecorder.js`
  - Wraps `navigator.mediaDevices.getUserMedia` and `MediaRecorder` for
    push-to-talk recording, chooses a supported MIME type when the browser
    exposes `MediaRecorder.isTypeSupported`, and stops all tracks on stop,
    cancel, or error.
- `webui/src/lib/desktopBridge.js`
  - `isDesktop()` — true when `window.location.search` includes
     `accessor=desktop` AND `window.pywebview.api` is present.
  - `isDesktopAccessor()` — true when the Desktop accessor URL parameter is
    present, even before pywebview has injected the bridge.
  - `waitForDesktopBridge()` — waits for pywebview's `pywebviewready` DOM event
    and resolves false after a short timeout, so Desktop-only UI is not
    permanently hidden when the bridge is injected after Svelte mount.
  - `getDesktopCapabilities()` — calls the bridge and returns
     `{ wakeword: true }` inside the Desktop shell; caches the result.
  - `hasWakeword()` — convenience wrapper that resolves to the `wakeword`
    capability flag.
  - `getWakewordStatus()` — polls the bridge for the full wakeword status
    dict; returns `{ enabled: false, state: 'off' }` when the bridge is
    absent.
  - `setWakewordEnabled(enabled)` / `setWakewordConfig(config)` — call the
    bridge with proper error handling.
  - `onWakewordStatusChange(callback, intervalMs)` — starts a 500ms polling
    interval that calls `callback(status)` whenever the full status payload
    changes, including config-only changes where `state` is unchanged. Returns
    a cleanup function. Only active when `isDesktop()` is true.
- `webui/src/lib/wakewordSettings.js`
  - Pure helpers for the Settings → Voice panel:
    `createVoiceSettingsState()`, `applyWakewordStatus(state, status)`,
    `buildVoiceSettingsPayload(state, lastSaved)`, `voiceSettingsDirty(state, lastSaved)`,
    `snapshotVoiceSettings(state)`.
  - Defaults match the Desktop wakeword config schema: `enabled: false`,
    `engine: 'openwakeword'`, `sensitivity: 0.5`, etc.
  - `liveState` tracks the worker state string from the bridge and is
    excluded from save payloads (it is read-only).
- `webui/src/components/ToastStack.svelte`
  - Renders dismissable toast notifications from toast state using the shared
    toast CSS classes.
- `webui/src/components/ChatView.svelte`
  - Loads chat history in pages: the initial session load asks `chat.history`
    for the newest 100 visible messages, and top-of-timeline pagination requests
    50 older messages at a time.
  - Loads `chat.commands` on mount and passes the flat combined command/available-skill list to the composer as the existing `availableSkills` prop shape for trigger suggestions.
  - Shows inline neutral `actionInfo` feedback when `chat.stream` handles a built-in command without starting a Run.
  - When a manual `/compact` command is handled without starting a Run, ChatView reloads the active session history so the new compaction separator appears immediately.
  - When a handled `/new` command includes `{ data: { command: "new", session_id } }`, ChatView updates the selected Agent's current Session locally, clears any Session override, and loads the new Session history.
  - `/retry` follows the normal Run response path: when `chat.stream` returns a Run/SSE payload, ChatView starts the Run locally and subscribes to its SSE URL instead of treating it as command-handled feedback.
  - When the backend rejects `/compact` because the target Session already has an active Run, ChatView surfaces the handled reply inline and does not start a second Run.
  - Sends message submits to the server even while the Session has an active Run. When `chat.stream` returns `{ queued: true, item }`, ChatView adds that server queue item locally and does not open an SSE subscription for it.
  - Refreshes queued-message state from `chat.queue_list` after history load and after terminal Run events so the accessor reflects the server-owned queue after reloads, reconnects, and drain transitions.
  - When history reload returns an `active_run` for the same Run already tracked
    locally, ChatView merges the returned retained Run events before deciding
    whether to resubscribe, so switching away from and back to a running Session
    catches up SSE-only deltas that were not sent over WebSocket.
  - Owns the session drawer toggle plus local `viewingSessionId` override state.
    Selecting a session from the drawer loads its history without mutating the
    Agent's persisted `current_session_id`.
  - Sub-agent session links keep the globally selected Agent on the parent Agent
    and route the displayed child Session through a local Agent/Session override,
    so “Return to current session” returns to the parent Agent's current Session.
    Repeated clicks on the same sub-agent session link are treated as distinct
    navigation requests, so returning to the parent and opening that same child
    Session again reloads it.
  - Shows a mic status indicator in the header-right toolbar when
    `desktopCapabilities?.wakeword` is true. The indicator is an 8px colored
    dot with tooltip showing the current wakeword worker state:
    gray = disabled, green pulsing = listening, orange = recording,
    accent spinner = transcribing/sending, red = error. `wakeword_detected`
    shares the listening treatment with a distinct tooltip. Clicking navigates
    directly to Settings → Voice.
- `webui/src/components/QueuedMessages.svelte`
  - Renders queued server-backed messages with remove and inline edit controls. Edit mode is local UI state; save persists through `chat.queue_update` and cancel only exits the local editor.
- `webui/src/components/SessionListDrawer.svelte`
  - Renders the per-Agent session list with platform badges, last-active
    metadata, selection callbacks, and inline retroactive channel-linking flows
    backed by `session.link_channel`.
- `webui/src/components/ChatComposer.svelte`
  - Supports `/skill-name` at the start of input and `$skill-name` inline autocomplete. Selection inserts only the trigger token and preserves the rest of the message text exactly; backend chat activation handles loading.
  - Built-in command autocomplete consumes bare command names from `chat.commands` and inserts the `/` prefix exactly once at compose time.
  - Slash-trigger autocomplete shows the combined command/skill catalog from `chat.commands`; dollar-trigger autocomplete filters that catalog to skills only because `$skill-name` is a skill-only convention.
  - Supports attachment uploads via file picker, image paste, and drag-and-drop.
  - Includes a microphone button for push-to-talk recording. Starting requests
    microphone access, stopping uploads the recorded blob to
    `/api/speech/transcribe`, and successful transcription inserts text into the
    composer without sending automatically. Existing draft text is preserved and
    the transcript is appended on a new line. The next submit carries
    `{ inputOrigin: "speech_transcription" }` through ChatView, which maps it to
    RPC `input_origin: "speech_transcription"` so the backend can add a hidden
    system reminder for the model.
  - Cancels active recording and stops media tracks on submit and component
    destroy. Unsupported browser APIs, permission failures, and missing STT
    configuration surface through existing Chat action/toast feedback.
  - Maintains local pending attachments with `preview_url` object URLs and builds
    canonical message `content` as `string` or `list[ContentBlock]` on send.
  - The visible composer box focuses the textarea from its padded wrapper area,
    not only from the text glyph area. After a successful send, the textarea
    value and height reset to the single-line composer state. The empty
    textarea is top-aligned within the visible box so typing begins at the top
    of the field while composer action buttons stay bottom-aligned.
- `webui/src/components/SkillAutocomplete.svelte`
  - Renders the flat name/description list selected by the composer for the active trigger context: combined commands plus skills for `/`, skills only for `$`. Skills with validation warnings are still loadable and may appear; invalid/non-loadable diagnostics are excluded by ChatView data flow.
- `webui/src/components/WakewordVoiceSettings.svelte`
  - Gated on `desktopCapabilities?.wakeword` — renders nothing when absent.
  - Self-contained Settings sub-panel that manages wakeword state and saves
    through the Desktop bridge (not server RPC).
  - Controls: enable/disable toggle (calls `setWakewordEnabled`), sensitivity
    slider (0–1, step 0.05), target Agent dropdown, session behavior dropdown
    (`active` / `new`), plus read-only displays for engine, microphone,
    wake phrase, and live worker state with a colored dot.
  - Live state dot colors: green pulsing = listening, orange = recording,
    accent spinner = transcribing/sending, red = error, gray = off.
  - Auto-saves sensitivity, agent, and session behavior changes through
    `setWakewordConfig()` with 800ms debounce; emits toast feedback through
    the app-level `ToastStack`.
  - Privacy note paragraph rendered from i18n key
    `settings.voice.privacyNote`.
- `webui/src/lib/agentForm.js`
  - Normalizes Agent create/update form values into RPC payloads. Workspace is
  omitted from public create payloads but included in edit payloads when changed.
  - In edit mode it builds sparse update payloads: unchanged fields are omitted
    so inherited resolved defaults are not written back as explicit overrides.
    Clearing `temperature` or `thinking_effort` sends `null`; clearing model
    fields sends `""`.
  - Includes `custom_system_prompt_enabled` as a boolean editable field and
    `memory_prompt_mode` as an enum (`off`, `agent`, `agent_user`). In edit mode
    unchanged values are omitted from sparse update payloads.
- `webui/src/components/AgentsView.svelte`
  - Loads `agent.list` plus the backend catalogs from `model.list`,
    `connection.list`, `tool.list`, and `skill.list` on mount.
  - Calls `model.list` without capability/context filters so all models from
    usable configured providers remain visible. Local OpenAI-compatible
    providers such as future Ollama or LM Studio integrations may expose sparse
    or user-tuned catalog facts, and the Agent selector must not hide them based
    on missing tool/context metadata.
  - The Agent edit form uses backend-backed selects for `model`,
    `fallback_model`, and `thinking_effort`, plus a tool-toggle list sourced
    from `tool.list`. The tool-toggle list omits `memory`; the Memory dropdown
    owns both prompt-visible pinned memory and the effective memory tool.
    The simple Thinking Effort dropdown must escape the Model card clipping and
    stack above the following System Prompt card while open.
  - The Agent detail pane exposes only an on/off `Custom system prompt` toggle
    for Agent-scoped prompt editing plus a Memory dropdown that controls whether
    the runtime prompt includes no pinned memory, only `MEMORY.md`, or
    `MEMORY.md + USER.md`. It does not expose prompt-fragment editors inside the
    Agents view. The Memory dropdown panel must escape the System Prompt card's
    rounded clipping so all options remain visible near the card edge.
  - The Agent edit form shows workspace once in the Identity section as an
    editable text field. Workspace changes are saved through `agent.update` and
    are not duplicated again in the Access section.
  - The `New` action opens a compact modal instead of switching the detail pane
    into the full editor. The modal collects only Agent ID, name, model,
    thinking effort, and temperature; advanced access/fallback/session fields
    are configured after creation from the normal edit detail pane.
  - The Agent skill section is backend-backed by `skill.list`. Loadable skills are shown with name, description, warning text, availability state, requirement details, and toggles that write array-based `allowed_skills`. Non-loadable skills from `invalid_skills` render in a separate unavailable list with warnings and no toggles.
  - In edit mode, the primary save action lives as a normal footer at the bottom
    of the detail content, not as a floating/sticky control. The destructive
    delete action stays in the top detail action cluster.
  - Save/create/update/delete success feedback is sent through the app-level
    `ToastStack`, not through an inline success notice in the detail pane.
  - Model and fallback-model selects store both the canonical public model ID and
    selected connection. If a provider has one usable connection, the label stays
    as the model ID (for example `openrouter/anthropic/claude-sonnet-4`). If a
    provider has multiple usable connections, the label adds the connection
    label suffix (for example `openai/gpt-5.4 (OAuth)`). The Model section does
    not repeat the selected fallback model in a second read-only Fallback row.
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
  - Normalizes Recall settings from `settings.recall`, derives backend dropdown options from `available_backends`, and builds `settings.update({ recall: { backend } })` payloads.
  - Normalizes Web Search settings from `settings.web_search`, derives provider
    dropdown options from `available_providers`, and builds
    `settings.update({ web_search: { provider, searxng: { base_url } } })`
    payloads.
- `webui/src/components/SettingsView.svelte`
  - Includes a Defaults panel. It lets users edit project-wide Agent fallback
    values for `model`, `fallback_model`, `temperature`, and
    `thinking_effort` through `settings.update({ defaults: { agent: ... } })`.
    `temperature` supports explicit clear-to-null; `thinking_effort`
    distinguishes between no default and provider default.
    `model` and `fallback_model` use the same backend-backed searchable model
    picker as Agents, and `thinking_effort` uses the shared simple dropdown
    rather than a native select. Temperature is cleared by emptying the number
    field; there is no separate inline clear button.
  - Includes a Skills panel. It displays the default data-directory skill path as read-only and lets users add, remove, and save extra `skill_directories` entries through `settings.update`.
  - Includes a Sub-Agents panel. It lets users edit `max_subagent_depth`, `max_subagents_per_turn`, and `subagent_timeout_minutes` through `settings.update`.
  - Includes a Compaction panel. It lets users edit `auto`, `threshold`, `tail_tokens`, and `summary_model` through `settings.update`. `summary_model` uses the same backend-backed searchable model picker as Agents, with the empty value meaning the active Agent model.
  - Includes a Recall panel. It lets users choose the `session_search` recall backend with a simple dropdown backed by `settings.recall.available_backends`.
  - Includes a Web Search panel. It lets users choose the provider used by the
    `web_search` tool with a simple dropdown backed by
    `settings.web_search.available_providers`; selecting SearXNG reveals a base
    URL field persisted as `settings.web_search.searxng.base_url`.
  - Includes a Specialized Models panel. It renders Speech to Text and Text to
    Speech rows backed by `task_model.list_targets`, shows backend-owned option
    schemas from `task_model.options`, and saves sparse bindings through
    `task_model.update`. Target lists are credential-gated by the backend.
  - The Appearance, Skills, Sub-Agents, Compaction, and Recall panels auto-save about
    800 ms after the last dirty edit. Their manual save buttons remain visible
    in sticky footers inside the panel scroll area, stay enabled for trust, save
    immediately when dirty, and show an "Already saved" success toast when the
    panel is already clean.
  - Transient save/copy/OAuth feedback is emitted through the app-level
    `ToastStack`; Settings must not render a separate local toast box for these
    messages.
  - Includes a Channels panel. It lists configured channels, hydrates per-row
    running state via `channel.status`, and supports create, edit, delete,
    enable, and disable actions through the `channel.*` RPC methods.
  - Includes a Voice panel (Desktop-only). Gated on `desktopCapabilities?.wakeword`;
    normal browser WebUI instances must not show the Voice panel in Settings.
    Renders `WakewordVoiceSettings` which saves through the Desktop bridge
    (`setWakewordConfig`, `setWakewordEnabled`) rather than server RPC.
    The panel auto-saves sensitivity, agent, and session behavior with 800ms
    debounce; the enable toggle saves immediately. Manual save button shows
    "Already saved" when clean. Read-only fields (engine, microphone, wake
    phrase) display the current Desktop configuration.
  - The Providers panel renders OAuth connections with Connect/Disconnect
    controls only when the settings connection metadata has `connectable: true`.
    Static OAuth-token connections without Device Flow metadata render like
    credential status rows instead of offering a dead Connect action.
    `provider.connect` opens an inline Device Flow dialog with the user code,
    copy control, and verification link; `provider_auth_completed` closes it
    and refreshes settings. Settings sends public compositional connection IDs
    such as `github-copilot:oauth` in provider RPC payloads.
- `webui/src/components/ChatTimeline.svelte`
  - When older history is available, scrolling to the top calls back to ChatView
    to prepend older messages and preserves the user's scroll anchor after the
    DOM grows above the viewport.
  - Shows day separators only when the visible Session timeline spans more than
    one local calendar day. In multi-day histories, each day group gets its own
    separator and the current local day is labeled with `chat.today` (`Today`).
  - When the user submits a message that starts a new Run, the Timeline anchors
    the submitted user message at the top of the chat scrollport so the agent
    response begins below it instead of starting pinned to the bottom edge. The
    Timeline keeps only the dynamic bottom scroll space still needed for that
    alignment, shrinking it as assistant/reasoning/tool output fills the
    viewport so the user message can remain at the top without a fixed blank
    scroll tail.
  - Renders tool-call summary text from
    `tool_call_started.payload.display.summary` when available. Legacy
    argument-label fallbacks exist only for history or old events without a
    display payload; empty summaries render no parenthesized argument text.
    Long summaries may truncate only the argument value itself; fixed row
    markers such as the closing parenthesis and duration/status text must remain
    visible.
  - Renders Assistant Run duration in the run header and completed tool-call
    duration in tool rows. It prefers `timing.duration_ms` from live events or
    persisted history and only reconstructs from event timestamps as a fallback.
    Cancelled tool rows continue to show the cancelled status label.
  - Renders `subagent` and `subagent_result` tool calls with a Sub-Agent label, target Agent identifier, compact argument preview, status text, and a session navigation link when the tool result includes `agent_id` and `session_id`. When a later `subagent_result` completes the same child session, the original `subagent` spawn row should no longer appear stuck in `running` state.
  - Merges live `subagent_session_started` Run events into the matching
    `subagent` tool row so `view session` is available while a blocking child
    Session is still running, before the final tool result has returned.
  - Keeps Sub-Agent tool rows collapsed by default, including while running;
    the row itself remains visible with status and `view session`, and details
    open only when the user asks for them.
  - The Sub-Agent row dot reflects child work status when the tool result,
    matching `subagent_result`, or live child Run lifecycle event exposes it:
    queued/running stays orange, completed is green, and failed/cancelled use
    the corresponding error/cancelled states. If no child status is available,
    it falls back to the parent tool-call status.
  - Does not render streamed/provisional Sub-Agent rows while tool arguments are
    still streaming. Once a blocking `subagent` tool call has started, the row
    appears immediately as `starting`; `view session` appears as soon as the
    backend emits `subagent_session_started`. Non-blocking rows wait for a
    navigation target or final result so background calls do not show inert
    pseudo-rows.
  - Does not render streamed tool-call preparation as a standalone timeline
    card. Tool-call deltas stay inside the live assistant-run model until the
    real tool row is available.
  - Renders live Bash stdout/stderr deltas in dedicated tool detail rows. When
    the final Bash result also includes `data.output`, the Result row suppresses
    that duplicate output if live stdout/stderr are already present and shows
    only completion metadata such as status, exit code, and truncation. Reloaded
    history without live stdout/stderr still shows `data.output` in the Result
    row.
  - Renders automatic follow-up Runs triggered by Sub-Agent batch completion as
    distinct Assistant Run blocks with their normal Assistant title bar instead
    of adding a separate inline divider. History grouping must split consecutive
    Assistant messages into separate Run blocks unless a visible tool result
    sits between them, so this remains true after returning from a child Session
    or reloading history.
  - Renders `model_fallback` assistant-run children as a small inline informational notice using i18n text.
  - Renders `compaction_separator` timeline items as a date-separator-style inline notice using the `chat.compacted` i18n label.
  - Renders assistant streaming output through `renderMarkdownStreaming(...)` and settled assistant run output plus persisted assistant messages through `renderMarkdown(...)`, all inside a scoped `.msg-markdown` container so normal agent replies display headings, lists, links, tables, and code fences as Markdown instead of raw source while long open fenced blocks remain inspectable during streaming.
  - Renders user message content arrays block-by-block: `text` as normal message text,
    image `media` via `/api/attachments/<id>`, and `file` as download links.
  - User message text preserves plain text formatting but must wrap long
    unbroken tokens inside the bubble instead of creating horizontal overflow.
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
  - Loads `prompt.list` and uses its `scopes` list to render the prompt-scope
    selector. The selector shows `Default` plus enabled Agent scopes only.
    Selecting an Agent scope reloads fragments with `scope` in `prompt.list`.
  - Renders the editable prompt fragments with reset controls near the fragment
    header. Fragment cards must clip their surface backgrounds to the rounded
    card corners so header bars do not bleed past the radius.
  - Saves and resets use the selected prompt scope. Agent-scope reset asks for
    Agent-specific confirmation and restores the current Default fragment
    content into the Agent scope.
  - The preview section shows a normal Agent picker for Default scope and sends
    `prompt.preview` without a scope so the preview matches that Agent's
    effective runtime prompt. For an Agent scope it replaces the picker with the
    Agent scope chip and sends `prompt.preview` with `{ agent_id, scope }` for
    that Agent.
  - Dirty fragments auto-save about 800 ms after the last edit using per-fragment
    debounce timers.
  - Manual save is a single global button at the bottom of the System Prompt
    content. It saves all dirty fragments immediately, stays enabled for trust
    when the view is clean, and shows an "Already saved" success toast when
    there are no dirty fragments.
  - Transient save/reset/preview/copy feedback is emitted through the app-level
    `ToastStack`; System Prompt must not render a separate local toast box.
- `webui/src/App.svelte`
  - Owns app shell navigation and shares Agent selection/refresh state between
    Chat and Agents views.
  - Queues WebSocket Run lifecycle summaries before passing them into
    `ChatView`, so rapid internal follow-up Runs cannot overwrite earlier
    `run_started` or `run_output` events in the same render tick.
  - Routes the top-level `Cron` navigation item to `CronView` between Agents and
    System Prompt.
  - Routes the top-level `Logs` navigation item to `LogsView`.
  - Handles server-pushed `app_error` WebSocket events as error toasts.
  - Owns the app-level toast queue, auto-dismiss timers, and dismissal callback,
    and passes an `onToast` callback to tabs that need transient user feedback.
  - Forwards `provider_auth_completed` WebSocket events to `SettingsView` when
    the Settings view is active.
  - In Desktop accessor mode, waits for pywebview's bridge readiness event
    before loading Desktop capabilities. Clicking the Chat wakeword indicator
    sends a one-shot Settings target request for the Voice panel.

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
- Speech transcription in the Chat composer is an input aid only. It must not
  auto-send messages, persist recordings, or bypass the server speech endpoint.
- Streaming output is accessor-local/in-memory. `streamingItems` preserves the
  provider-visible order of reasoning, assistant text, and tool-call deltas;
  the final `assistant_output` event clears the buffer and becomes the
  authoritative rendered message.
- ChatView batches only high-frequency SSE delta events before updating local
  state. Stable Run lifecycle events such as tool start/result, sub-agent
  session links, assistant output, and terminal events flush immediately so
  short-lived tool states are visible in order.
- SSE reconnects use the highest contiguous sequence for the active Run, not
  the maximum seen sequence, because WebSocket lifecycle summaries intentionally
  omit SSE-only deltas and may otherwise cause reconnect replay to skip them.
- Assistant output Markdown rendering is accessor-side only. It applies to live
  streaming assistant text, completed assistant output, and persisted assistant
  history messages. User text, reasoning bodies, tool details, and error
  messages remain plain pre-wrapped text; user text additionally breaks long
  unbroken tokens within the message bubble.
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
- Built-in command handling feedback is accessor-local UI state only. When the backend returns `{ command_handled: true, reply }`, ChatView shows the reply inline and must not subscribe to a Run stream for that submit. The exception is not a handled command response: `/retry` returns a normal Run/SSE payload and must be handled like any other started Run.
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
- `System Prompt` is functional — it renders five fragment editors (`system.md`, `runtime.md`, `tools.md`, `channels.md`, `skills.md`) with reset/variable-reference, one global save button for all fragments, a scope selector for Default plus enabled Agent scopes, plus a preview section with agent picker/chip, refresh, copy, and token count. `Settings` is functional and contains the General (server host, data directory), Skills (default skill path and extra scan directories), Defaults (project-wide Agent fallback values), Sub-Agents, Compaction, Recall (session_search backend), Specialized Models (STT/TTS bindings), Providers (credential status, model counts, model database refresh), and Appearance (language preference) sub-panels. In the Agents view, model, tool, skill, and custom-system-prompt toggle controls are backend-backed, and new Agent creation starts in the compact modal before advanced editing.
- `Logs` is functional — it shows one selected daily log file, defaults to the
  newest file, keeps the current selection sticky when newer files appear, and
  applies level filtering, newest/oldest local ordering, and free-text search
  locally in the accessor.
- The production build emits `webui/dist`, which FastAPI serves when present.
