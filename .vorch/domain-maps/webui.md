# WebUI

The WebUI is vBot's Svelte accessor: it presents server-owned state and sends user intent through the server's HTTP, RPC, WebSocket, and SSE contracts.

## Overview

`webui/src/` owns browser presentation, interaction state, navigation, and transport adaptation. It does not own Chat, Provider, Extension, Project, or persistence semantics; those remain in their backend domains. `webui/src/lib/api.js` is the single raw transport seam: components and controllers use its typed-by-convention wrappers instead of spelling RPC method names or constructing protocol envelopes themselves.

The production server serves `webui/dist/`; source changes therefore require a frontend build before they appear through the packaged server. A missing build must remain a safe server state, not a startup failure.

## Interfaces and ownership

- `webui/src/App.svelte` and `webui/src/lib/appController.js` own the application shell: active view, global loading and availability state, server-event routing, resource refreshes, and composition of the major views. `components/AppShell.svelte` additionally owns the capability-gated Desktop context menu because it is global viewport/focus presentation; host clipboard and browser launch remain behind `lib/desktopBridge.js` rather than page-origin APIs.
- The Agents surface keeps `id` outside debounced `agent.update`: `AgentEditor.svelte` uses an explicit rename modal and `api.js::renameAgent`, then `AgentsView.svelte` replaces the roster entry and selection from the returned Agent. `AgentListPane.svelte` reorders the canonical Identity Agent roster through a dedicated drag handle or ArrowUp/ArrowDown; `AgentsView.svelte` optimistically projects the move, persists the complete id list with the latest `order_revision`, and reloads server truth on failure or conflict. Other connected windows consume Agents invalidation, remap rename scope before reconciliation when present, and reload the authoritative roster order without changing a still-valid selection.
- `webui/src/lib/chatState.js` owns Chat's reactive client projection and asynchronous workflows for roster, history, commands, sends, Queue mutations, continuation actions, and Run cancellation; `chatRunStream.js` owns stream reconciliation, while `ChatView.svelte` composes presentation, navigation consequences, and local input interactions.
- `webui/src/components/SettingsView.svelte`, its panel components, and `webui/src/lib/settingsView.js` own Settings forms and management surfaces. They submit backend contracts; they do not reinterpret domain policy.
- `webui/src/lib/projectsView.js` owns the Project-management state and workflows through `createProjectsState()` and `createProjectsController()`; `ProjectsView.svelte` renders that state and forwards user intents. The Project selected for management is separate from the Project context selected in Chat.
- `webui/src/lib/terminalsView.js` owns manual Terminal Session start and errors, live-plus-retained-session and server-persisted launch-history reconciliation, selected-stream sequence/gap recovery, exact xterm input batching, resize debounce, operator mutations, and cleanup; `TerminalsView.svelte` rebuilds bounded xterm.js scrollback from the authoritative snapshot after mount/navigation/reconnect, preselects the newest reusable command/arguments/working-directory configuration whenever New terminal opens, starts manual sessions in focused direct-control mode, keeps live Agent-owned sessions in observe mode until a primary click takes control, and presents finished retained sessions as read-only history.
- `webui/src/lib/api.js` normalizes transport and protocol failures as `ApiClientError`, exposes RPC wrappers, and owns WebSocket, Run-SSE, and log-stream subscriptions. Binary attachment delivery stays outside RPC envelopes.
- `webui/src/lib/i18n.js` and locale modules own user-visible copy. All visible strings, including labels, status text, validation, empty states, and error fallbacks, must go through i18n.

## Invariants and conventions

- The frontend is JavaScript, not TypeScript. Svelte 5 component communication uses callback props; do not add component event dispatchers.
- Browser controllers may optimistically preserve interaction continuity, but server responses and events remain authoritative. Queue contents, continuation availability, usage, connection state, and domain settings must reconcile to server truth.
- Run SSE carries the full ordered event stream for one Run. The app-wide WebSocket carries lifecycle summaries and invalidation signals, not high-volume Run or terminal deltas; a selected Terminal Session uses its own server-push WebSocket and rebuilds bounded retained scrollback plus the current screen from an authoritative ANSI snapshot after navigation, reconnect, or a sequence gap.
- Every browser resource has an explicit owner and cleanup path: EventSource, WebSocket, timers, observers, media streams, object URLs, document listeners, and desktop-bridge subscriptions must be released on teardown or replacement.
- Resource invalidation refreshes backing data without unexpectedly replacing an active draft, picker, modal, or form. Controllers defer visible swaps when the user is editing and apply the refreshed data at the next safe boundary.
- An Agents invalidation may carry an old→new Identity Agent mapping. Apply that mapping before `agent.list` reconciliation; otherwise the ordinary missing-selection fallback can silently switch a renamed Agent to the first roster entry.
- Dates and numbers shown to users use the active locale. Editable decimal settings remain text while being edited so comma-decimal input can be normalized deliberately at the payload boundary.
- Shared feedback and affordances stay shared: use `ToastStack` for transient operation results, established hint components for explanatory help, and existing deep modules under `webui/src/lib/` for stateful behavior rather than growing view components into alternate controllers.
- A feature View may keep DOM events, presentation-only derived values, and navigation callbacks, but multi-request sequencing, stale-response rejection, reconciliation, timers, and mutation error state belong to its existing controller. Extend that owner before adding a new helper, layer, or component-local workflow.
- Frontend behavior changes require focused Vitest coverage beside the relevant controller or component. Visual and interaction-system decisions additionally follow `.vorch/DESIGN.md`.

## Constraints and gotchas

- Debug UI and debug RPC access are development-only and must remain gated; a production browser must not expose tracing or probe surfaces.
- Only the Chat timeline owns Chat scrolling. Page-level or composer-level scrolling reintroduces competing scroll containers.
- Do not use native `title` attributes for product help; use the established accessible hint/tooltip patterns.
- Public UI contracts intentionally omit backend-only reasoning metadata such as `reasoning_meta`; do not leak it while rendering or persisting client state.
- Raw user or server content is rendered through safe text/Markdown/media paths. Do not introduce `innerHTML` with untrusted content or let attachment paths become filesystem addressing.

## References

Read these only when your task matches — not by default.

- Locating the owning frontend file, component, controller, style, or test, including Agents, Cron, System Prompt, Statistics, Logs, Debug, and shared UI work not covered below → `webui/source-map.md`
- Changing app startup, navigation, connection state, reconnect behavior, global server events, or resource invalidation → `webui/app-shell.md`
- Changing Chat selection, Sessions, history, Run streaming, Queue projection, timeline rendering, composer behavior, attachments, or speech input → `webui/chat.md`
- Changing Settings, Providers, Extensions, Skills, Agents, onboarding, appearance, logs, channels, or Desktop Voice surfaces → `webui/settings.md`
- Changing the Projects management view, Project discovery/editing, scan results, Team rows, or Project overrides → `webui/projects.md`
