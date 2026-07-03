# WebUI

Svelte accessor that talks only to the vBot server through HTTP RPC, Server-Sent Events, and WebSocket.

## Overview

`webui/` owns the browser interface. It imports no Python/core code, does not talk to providers directly, and treats the server as the source of truth for agents, sessions, runs, queues, settings, logs, channels, prompts, and debug data. The UI is an Agent-first app shell with Chat, Agents, Projects, Cron, System Prompt, Settings, Logs, and a Debug tab gated by `debug.enabled`; Voice controls are visible only inside the Desktop accessor when the pywebview bridge reports wakeword support. Built assets are emitted to `webui/dist`, which FastAPI serves when present.

The per-file breakdown of what each view/helper/primitive owns, and the deep per-feature runtime behavior, are task-gated — see References. This map keeps only the orientation, transport contracts, conventions, and invariants an agent needs before touching *anything* here.

## Transport & app state

- **One transport seam.** `webui/src/lib/api.js` is the boundary: RPC envelopes to `/api/rpc`, per-Run output over SSE, app-wide lifecycle over `/ws`, logs over `/ws/logs`, and attachments/speech over dedicated HTTP endpoints (`/api/upload`, `/api/attachments/{id}`, `/api/speech/*`). Binary payloads stay out of the RPC envelope by design.
- **`/ws` is server-push only** — clients send commands through `POST /api/rpc`, never the socket. On connect the server sends a `connection_ready` hello (bus `epoch`, `last_sequence`, `active_runs` snapshot); reconnect passes `epoch` + `after_sequence` for replay-or-live. The snapshot — not a buffer replay — is the source of truth for in-flight Run state on a fresh connect/reload.
- **SSE is the primary per-Run output stream.** `/ws` lifecycle summaries deliberately omit SSE-only deltas, so any catch-up logic must not assume `/ws` saw every Run sequence. All three reconnect loops (app WS, per-Run SSE, log stream) share one jittered exponential-backoff helper (`lib/backoff.js`).
- **Reload-on-change.** The generic `resource_changed` event drives per-kind refresh tokens (`lib/resourceInvalidation.js`); the `queue` kind is scope-routed (carries an `{agentId, sessionId}` scope), not a token. A surface fetches the fresh data in the background but defers the *visible* swap while its picker/form is busy, so an open selection or in-flight edit is never yanked.
- **Queue is a server-backed projection** keyed by (Agent, Session). The accessor may optimistically add/remove/update items, but `chat.queue_*` remains the source of truth — the WebUI must never implement a second send queue.
- **Override-first chat display.** An active session override (drawer pick or sub-agent link) wins over both the identity-current and the project-agent branches. Overrides are accessor-local, must offer a return path, and must never mutate the Agent's persisted current Session.
- **Project-agent addressing.** The identity path stays bare-id. A project agent is addressed `agent@projekt` for `chat.stream`/`chat.history`/`session.*`, and by **bare** id for the queue RPCs; `chat.cancel` keys on `run_id` only (see `webui/state-flows.md` → two-bar chat).

## Conventions

- Svelte code uses JavaScript, not TypeScript.
- Use Svelte 5 callback props for new component communication; do not add event dispatchers for new code.
- All visible strings go through `t(...)` in `webui/src/lib/i18n.js`. Add or update i18n tests when introducing new copy.
- Dates and times follow the app language, not the browser/OS locale: pass `activeLocaleTag()` from `i18n.js` to `Intl.DateTimeFormat`/`toLocaleString`, never the implicit `undefined` locale. Fractional numeric fields (temperature, compaction threshold) are `type="text"` + `inputmode="decimal"` inputs so the displayed separator stays the dot; their parsers accept a typed comma (`normalizeTemperature`, `normalizeAgentDefaultsTemperature`, `normalizeCompactionSettings`).
- Browser resources (`EventSource`, `WebSocket`, `MediaRecorder`, object URLs, timers, and polling intervals) need explicit cleanup on component destroy or state change.
- App-wide transient success/error feedback should use the app-level `ToastStack`; avoid new local toast systems inside individual views.
- Keep business-ish normalization in `webui/src/lib/*` helpers where it can be unit-tested, and keep Svelte components focused on display, input, and orchestration.
- Frontend tests use Vitest/jsdom and live near the source under `webui/src/**/__tests__/`. For frontend-only changes, use `python scripts/quality-frontend.py [paths...]` when a real code path changes; doc-only domain-map edits do not need Vitest.

## Constraints & Gotchas

- The Toasted `Components` showcase is a design/reference artifact only. It must not ship as a live WebUI tab or appear in normal navigation.
- `Debug` navigation is hidden unless `debug.enabled` is true; `DebugView` should not become the normal place to inspect chat/session internals.
- Full reload recovery for in-flight Runs is out of scope. The accessor restores the last selected Agent (and, inside a project, the last active project agent — see the two-bar chat note in `webui/state-flows.md`) through `localStorage`, the active tab through the URL hash, and a session override from the adopted top-of-stack history entry (see the browser-history note in `webui/state-flows.md`); on reload, old chat content comes from Session history plus the `connection_ready` snapshot (which is the new source of truth for in-flight Run state — the historical WS buffer-replay path is gone).
- `New Session` is blocked while the selected Agent/current Session has an active Run. Switching to another Agent while a Run is active is allowed.
- In Chat, only the message timeline should scroll. The Agent bar, notices, queued-message region, and composer stay visible inside the bounded view.
- `System Prompt`, `Settings`, and `Logs` are functional product views, not placeholders. Do not replace them with links to files or backend-only instructions.
- Attachments, speech audio, and generated speech artifacts intentionally stay outside the RPC envelope; do not force binary payloads through `rpc(...)`.
- WebSocket lifecycle summaries intentionally omit SSE-only delta events. Any reconnect or catch-up logic that assumes `/ws` saw every Run sequence will skip streamed content.
- `/ws` run lifecycle/output payloads and the `connection_ready` snapshot carry a bare `agent_id` plus the Run's `project_id` (`null` for identity). `chatRunStream` rebuilds the outside `agent@projekt` address with `formatAgentAddress(agent_id, project_id)` at its two ingestion seams — `runEventFromServerEvent` (per-event) and `applyConnectionSnapshot` (handshake snapshot) — so the `/ws` re-attach/catch-up path matches a project-agent session **state** keyed by its full address. The **sub-agent status projection is the deliberate exception:** its session-scoped keys (`session:`/`sessionTool:`/`sessionDuration:`) are written with the **bare** child agent id (`bareAgentIdForStatusKey` strips the `@projekt` suffix), because their readers are persisted spawn descriptors that only carry bare ids — session ids are globally unique, so bare keys cannot collide across projects. The RPC side of those rows goes the other way: ChatView qualifies a descriptor's bare child id with the **displayed session's** project (`qualifiedChildAgentAddress`) for the spawn-row navigation link, the dot-verification `chat.history`, and the result fetch, so a project run's children are addressed `child@projekt` while identity children pass through unchanged. An identity run has no `project_id`, so the rebuild yields the bare id and the identity path stays byte-identical.
- Public chat/history/Run rendering must not expose opaque provider metadata such as `reasoning_meta`; backend payload stripping is required, but UI code should not add debug escapes in normal views.
- The selected log file remains user-controlled. A newer file appearing in the catalog must not automatically move the active Logs view away from the user's current file.
- The production server serves `webui/dist` only when the built `index.html` exists; missing built assets must not break server startup.

## References

Read only when your task matches — not by default.

- Working on a specific view/component (what each file owns) → `webui/source-map.md`
- Deep chat/timeline/streaming/queue/navigation/panel behavior → `webui/state-flows.md`
