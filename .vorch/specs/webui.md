# WebUI

Svelte accessor that talks only to the vBot server through HTTP RPC, Server-Sent Events, and WebSocket.

## Overview

`webui/` owns the browser interface. It does not import Python/core code and it
does not talk to providers directly. The minimal Phase 4 product presents an
Agent-first chat surface, Agent management, and placeholders for System Prompt
and Settings.

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
- `webui/src/lib/agentForm.js`
  - Normalizes Agent create/update form values into RPC payloads. Workspace is
  displayed from Agent data but omitted from public create/update payloads in
  Phase 4.
- `webui/src/components/AgentsView.svelte`
  - Loads `agent.list` plus the backend catalogs from `model.list`,
    `connection.list`, and `tool.list` on mount.
  - The Agent form uses backend-backed selects for `model`, `fallback_model`,
    and `thinking_effort`, plus a tool-toggle list sourced from `tool.list`.
  - Model and fallback-model selects store both the canonical public model ID and
    selected connection. If a provider has one usable connection, the label stays
    as the model ID (for example `openrouter/anthropic/claude-sonnet-4`). If a
    provider has multiple usable connections, the label adds the connection
    label suffix (for example `openai/gpt-5.4 (OAuth)`).
- `webui/src/lib/settingsView.js`
  - Normalizes Settings provider metadata that now uses credential-centric
    fields (`credential_key`, `credentials_configured`) rather than env/API-key
    wording.
  - Skills remain textarea-based until a backend skill catalog exists.
- `webui/src/App.svelte`
  - Owns app shell navigation and shares Agent selection/refresh state between
    Chat and Agents views.

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
- `System Prompt` and `Settings` are placeholders in Phase 4. In the Agents
  view, model and tool catalogs are now backend-backed; skills still remain
  placeholder/textarea-based until a backend skill catalog exists.
- The production build emits `webui/dist`, which FastAPI serves when present.
