# WebUI

Svelte accessor that talks only to the vBot server through HTTP RPC, Server-Sent Events, and WebSocket.

## Overview

`webui/` owns the browser interface. It does not import Python/core code and it
does not talk to providers directly. The minimal Phase 4 product presents an
Agent-first chat surface, Agent management, and placeholders for System Prompt
and Settings.

## Layout

- The WebUI uses a two-pane app shell: navigation on the left and content on
  the right.
- The left navigation contains at least these entries:
  - `Chat`
  - `Agents`
  - `System Prompt`
  - `Settings`
- Additional navigation entries may be added later.

## Interfaces

- `webui/src/lib/api.js`
  - `rpc(method, params?, options?)` posts to `/api/rpc` and returns `result` or
    throws `ApiClientError` with a stable `code`.
  - `subscribeRunEvents(sseUrl, handlers, options?)` opens an `EventSource` for
    one Run timeline and returns `{ close, source }`.
  - `subscribeServerEvents(handlers, options?)` opens `/ws` and returns
    `{ close, socket }`.
- `webui/src/lib/i18n.js`
  - `t(key, fallback?, values?)` is required for all user-visible strings.
- `webui/src/lib/chatState.js`
  - Pure helpers for selected Agent, per-Agent/current-Session state, visible
    timeline items, active Run status, and FIFO queued messages.
- `webui/src/lib/agentForm.js`
  - Normalizes Agent create/update form values into RPC payloads. Workspace is
    displayed from Agent data but omitted from public create/update payloads in
    Phase 4.
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

## Constraints & Gotchas

- Reload recovery for in-flight Runs and accessor-local last-selected-Agent
  restore are out of scope for Phase 4.
- `New Session` is blocked while the selected Agent/current Session has an active
  Run. Switching to another Agent while a Run is active is allowed.
- `System Prompt` and `Settings` are placeholders in Phase 4.
- The production build emits `webui/dist`, which FastAPI serves when present.
