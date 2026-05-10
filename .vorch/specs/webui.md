# WebUI

Svelte accessor that talks only to the vBot server through HTTP RPC, Server-Sent Events, and WebSocket.

## Overview

`webui/` owns the browser interface. It does not import Python/core code and it
does not talk to providers directly. The product presents an Agent-first chat surface, Agent management, a functional Settings view with General, Skills, Providers, and Appearance sub-panels, and a functional System Prompt tab.

## Layout

- The WebUI uses the Toasted two-pane app shell: fixed 210px navigation on the
  left and content on the right.
- The left navigation contains at least these entries:
  - `Chat`
  - `Agents`
  - `System Prompt`
  - `Settings`

## Interfaces

- `webui/src/lib/api.js`
  - `rpc(method, params?, options?)` posts to `/api/rpc` and returns `result` or
    throws `ApiClientError` with a stable `code`.
  - `subscribeRunEvents(sseUrl, handlers, options?)` opens an `EventSource` for
    one Run timeline and returns `{ close, source }`. It subscribes to whole
    Run events plus streaming delta events and supports optional
    `afterSequence` URL construction for manual replay; native reconnect uses
    SSE event IDs / `Last-Event-ID` from the server.
  - `subscribeServerEvents(handlers, options?)` opens `/ws` and returns
    `{ close, socket }`. Supports `afterSequence` option for reconnect replay.
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
  - `role: "error"` history messages and live `error_message_persisted` Run events
    render as standalone message timeline items, never inside an assistant Run.
- `webui/src/lib/toastState.js`
  - Pure helpers for app-level toast state: `createToastState()`, `addToast(...)`,
    and `dismissToast(...)`.
- `webui/src/components/ToastStack.svelte`
  - Renders dismissable toast notifications from toast state using the shared
    toast CSS classes.
- `webui/src/components/ChatView.svelte`
  - Loads `skill.list` on mount and passes the loadable `skills` array (not `invalid_skills`) to the composer for skill-trigger suggestions.
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
  - Settings provider metadata includes `models_endpoint`; when at least one
    provider is refresh-capable and credential-configured, the Providers panel
    shows one global model database refresh control. It calls `model.refresh_db`
    without `provider_id` and then re-requests `model.list` after success.
  - Normalizes skill directory settings from `settings.skills.directories` and builds update payloads for `settings.update`.
- `webui/src/components/SettingsView.svelte`
  - Includes a Skills panel. It displays the default data-directory skill path as read-only and lets users add, remove, and save extra `skill_directories` entries through `settings.update`.
- `webui/src/App.svelte`
  - Owns app shell navigation and shares Agent selection/refresh state between
    Chat and Agents views.
  - Handles server-pushed `app_error` WebSocket events as error toasts.

## Conventions

- Svelte code uses JavaScript, not TypeScript.
- Use Svelte 5 callback props for component communication; do not use event
  dispatchers for new components.
- All visible text goes through `t(...)`; add i18n keys with tests when new UI
  copy is introduced.
- Browser resources (`EventSource`, `WebSocket`) must expose explicit cleanup and
  be closed on component destroy.
- The UI selects Agents, not Sessions. The shown chat is the selected Agent's
  `current_session_id`; old Sessions are not listed in Phase 4.
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

- Reload recovery for in-flight Runs and accessor-local last-selected-Agent
  restore are out of scope for Phase 4.
- `New Session` is blocked while the selected Agent/current Session has an active
  Run. Switching to another Agent while a Run is active is allowed.
- `System Prompt` is functional — it renders four fragment editors (`system.md`, `runtime.md`, `tools.md`, `skills.md`) with save/reset/variable-reference, plus a preview section with agent picker, refresh, copy, and token count. `Settings` is functional and contains four sub-panels: General (server host, data directory), Skills (default skill path and extra scan directories), Providers (credential status, model counts, model database refresh), and Appearance (language preference). In the Agents view, model, tool, and skill catalogs are backend-backed.
- The production build emits `webui/dist`, which FastAPI serves when present.
