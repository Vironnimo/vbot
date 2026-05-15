# WebUI

Svelte accessor that talks only to the vBot server through HTTP RPC, Server-Sent Events, and WebSocket.

## Overview

`webui/` owns the browser interface. It does not import Python/core code and it
does not talk to providers directly. The product presents an Agent-first chat surface, Agent management, a functional Settings view with General, Skills, Sub-Agents, Providers, and Appearance sub-panels, a functional System Prompt tab, and a functional Logs tab for read-only daily log viewing.

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
  - `listSessions(agentId, options?)` calls `session.list` and returns
    `{ sessions }` for one Agent.
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
- `webui/src/lib/toastState.js`
  - Pure helpers for app-level toast state: `createToastState()`, `addToast(...)`,
    and `dismissToast(...)`.
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
  - Loads `skill.list` on mount and passes the loadable `skills` array (not `invalid_skills`) to the composer for skill-trigger suggestions.
  - Owns the session drawer toggle plus local `viewingSessionId` override state.
    Selecting a session from the drawer loads its history without mutating the
    Agent's persisted `current_session_id`.
- `webui/src/components/SessionListDrawer.svelte`
  - Renders the per-Agent session list with platform badges, last-active
    metadata, selection callbacks, and inline retroactive channel-linking flows
    backed by `session.link_channel`.
- `webui/src/components/ChatComposer.svelte`
  - Supports `/skill-name` at the start of input and `$skill-name` inline autocomplete. Selection inserts only the trigger token and preserves the rest of the message text exactly; backend chat activation handles loading.
- `webui/src/components/SkillAutocomplete.svelte`
  - Renders loadable skill name/description suggestions for composer trigger contexts. Skills with validation warnings are still loadable and may appear; invalid/non-loadable diagnostics are excluded by ChatView data flow.
- `webui/src/lib/agentForm.js`
  - Normalizes Agent create/update form values into RPC payloads. Workspace is
  displayed from Agent data but omitted from public create/update payloads in
  Phase 4.
- `webui/src/components/AgentsView.svelte`
  - Loads `agent.list` plus the backend catalogs from `model.list`,
    `connection.list`, `tool.list`, and `skill.list` on mount.
  - The Agent form uses backend-backed selects for `model`, `fallback_model`,
    and `thinking_effort`, plus a tool-toggle list sourced from `tool.list`.
  - The Agent skill section is backend-backed by `skill.list`. Loadable skills are shown with name, description, warning text, and toggles that write array-based `allowed_skills`. Non-loadable skills from `invalid_skills` render in a separate unavailable list with warnings and no toggles.
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
  - Normalizes Sub-Agent settings from `settings.subagents` and builds update payloads for `settings.update`.
- `webui/src/components/SettingsView.svelte`
  - Includes a Skills panel. It displays the default data-directory skill path as read-only and lets users add, remove, and save extra `skill_directories` entries through `settings.update`.
  - Includes a Sub-Agents panel. It lets users edit `max_subagent_depth`, `max_subagents_per_turn`, and `subagent_timeout_minutes` through `settings.update`.
  - Includes a Channels panel. It lists configured channels, hydrates per-row
    running state via `channel.status`, and supports create, edit, delete,
    enable, and disable actions through the `channel.*` RPC methods.
  - The Providers panel renders OAuth connections with Connect/Disconnect
    controls. `provider.connect` opens an inline Device Flow dialog with the user
    code, copy control, and verification link; `provider_auth_completed` closes
    it and refreshes settings. Settings sends public compositional connection IDs such as
    `github-copilot:oauth` in provider RPC payloads.
- `webui/src/components/ChatTimeline.svelte`
  - Renders `subagent` and `subagent_result` tool calls with a Sub-Agent label, target Agent identifier, compact argument preview, status text, and a session navigation link when the tool result includes `agent_id` and `session_id`.
  - Renders `model_fallback` assistant-run children as a small inline informational notice using i18n text.
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
  `current_session_id`. The read-only banner provides an explicit “Return to
  current session” action, and normal new-session/agent-selection actions also
  clear the override.
- Queue state is accessor-local/in-memory and scoped by Agent plus current
  Session. Queued messages are visible and removable before send.
- Streaming output is accessor-local/in-memory. `streamingItems` preserves the
  provider-visible order of reasoning, assistant text, and tool-call deltas;
  the final `assistant_output` event clears the buffer and becomes the
  authoritative rendered message.
- Visible chat rendering treats an assistant Run as one assistant block. Tool
  lifecycle events (`tool_call_started` and `tool_call_result`) are merged into a
  single expandable tool row inside that block rather than rendered as separate
  chat messages.
- Visible chat history accepts normal user, assistant, tool, and persisted error
  messages; kernel-internal note/system-reminder entries must be filtered out if
  they ever arrive from a server response.
- Persisted error messages are visible chat timeline items with an error label and
  distinct red treatment.
- Skill trigger autocomplete is a composer aid only. It must not fetch or inject skill content directly; `/skill-name` and `$skill-name` text is sent unchanged for backend deterministic activation.
- Partial tool-call argument JSON may be accumulated internally but must not be
  displayed as final normal UI data before the complete `tool_call_started`
  event arrives.
- In Chat, only the message timeline should scroll. The agent bar, notices,
  queued-message region, and composer stay visible inside the bounded view.

## Constraints & Gotchas

- The Toasted `Components` showcase is a design/reference artifact only. It
  must not ship as a live WebUI tab or appear in normal navigation.
- Reload recovery for in-flight Runs and accessor-local last-selected-Agent
  restore are out of scope for Phase 4.
- `New Session` is blocked while the selected Agent/current Session has an active
  Run. Switching to another Agent while a Run is active is allowed.
- `System Prompt` is functional — it renders four fragment editors (`system.md`, `runtime.md`, `tools.md`, `skills.md`) with save/reset/variable-reference, plus a preview section with agent picker, refresh, copy, and token count. `Settings` is functional and contains four sub-panels: General (server host, data directory), Skills (default skill path and extra scan directories), Providers (credential status, model counts, model database refresh), and Appearance (language preference). In the Agents view, model, tool, and skill catalogs are backend-backed.
- `Logs` is functional — it shows one selected daily log file, defaults to the
  newest file, keeps the current selection sticky when newer files appear, and
  applies level filtering, newest/oldest local ordering, and free-text search
  locally in the accessor.
- The production build emits `webui/dist`, which FastAPI serves when present.
