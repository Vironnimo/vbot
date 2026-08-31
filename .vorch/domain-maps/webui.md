# WebUI

The WebUI is vBot's Svelte accessor: it presents server-owned state and sends user intent through the server's HTTP, RPC, WebSocket, and SSE contracts.

## Overview

`webui/src/` owns browser presentation, interaction state, navigation, and transport adaptation - never Chat, Provider, Extension, Project, or persistence semantics. `lib/api.js` is the single raw transport seam: components use its wrappers instead of spelling RPC names or building envelopes. The production server serves `webui/dist/`, so source changes need a build; a missing build stays a safe server state, not a startup failure.

## Ownership

- `App.svelte` + `appController.js` own the shell: active view, global loading/availability, server-event routing, resource refreshes, view composition. `AppShell.svelte` adds the capability-gated Desktop context menu (global viewport/focus concern); host clipboard/browser launch stay behind `desktopBridge.js`.
- Chat: `chatState.js` owns the reactive projection and workflows, `chatRunStream.js` stream reconciliation, `ChatView.svelte` presentation plus navigation consequences - detailed in `webui/chat.md`.
- Settings forms (`SettingsView.svelte` + panels + `settingsView.js`) submit backend contracts without reinterpreting domain policy. The dedicated Skills management view (`components/skills/` + `skillsView.js`) owns the Configure -> Skills tab: inventory read model, policy mutations (`skill.set_disabled`/`skill.share`), global/private create-edit-delete in modals, search plus by-source/by-agent grouping, and the scan-directory rows - Settings keeps no Skill surface.
- Projects: `projectsView.js` state/controller, `ProjectsView.svelte` rendering. The management Project is separate from Chat's working Project context.
- Terminals: `terminalsView.js` owns start/errors, live-plus-retained reconciliation with launch history, sequence/gap recovery, authoritative TUI-mode snapshots, per-terminal input batching, mutations, geometry, cleanup. Hardening: a socket stuck connecting past 8 s force-closes into normal reconnect; PTY frames pass through a stateful sanitizer reassembling escape sequences split across frames (held tails reset on `terminal_ready`) rewriting nothing else - the server-side renderer consumes the same bytes. Tiles rebuild bounded scrollback from the authoritative snapshot after mount/reconnect/resync, fit via debounced clamped resize adopting server-confirmed dimensions, preselect the newest reusable config for new terminals, and render finished retained sessions read-only. Dimensions change only through explicit start/resize.
- `api.js` normalizes transport failures as `ApiClientError` and owns WS/SSE/log subscriptions; binary attachments stay outside RPC envelopes. `i18n.js` owns every visible string.
- `api.js` owns `session_store.status`, `session_store.snapshot_create`, and `session_store.incident_acknowledge` wrappers. `App.svelte` loads the safe Session-store projection on connection and `resource_changed(kind="session_store")`, keeps the last incident during transient refresh failure, and renders the unacknowledged recovery incident as a sticky global `Banner`; acknowledgement updates state only from the returned authoritative projection.

## Invariants

- JavaScript, no TypeScript; Svelte 5 callback props, no event dispatchers.
- Optimistic continuity is allowed; server responses and events stay authoritative - Queue contents, continuation availability, usage, connection state, and settings reconcile to server truth.
- Run SSE carries one Run's full stream; the app-wide WebSocket carries lifecycle/invalidations only; a selected Terminal uses its dedicated socket, rebuilding from the ANSI snapshot after navigation, reconnect, gaps, TUI boundaries, or end.
- Every browser resource has an owner and cleanup path (EventSource/WebSocket/timers/observers/media/object URLs/listeners/bridge subscriptions).
- Invalidation refreshes backing data without yanking active drafts/pickers/modals/forms - controllers defer visible swaps to safe boundaries. Agents invalidations carrying an old->new mapping apply before reconciliation (otherwise rename silently falls back to first roster entry); `memories` invalidation refetches only while its disclosure is open.
- Locales format dates/numbers; editable decimal settings stay text while edited so comma decimals normalize deliberately at payload boundary.
- Shared feedback stays shared (`ToastStack`, hint components, existing lib modules) - extend the owning controller instead of growing views into alternate controllers. Frontend behavior changes bring focused Vitest coverage; visual decisions follow `.vorch/DESIGN.md`.

## Constraints & gotchas

- Debug UI/RPC access is development-only and gated - production browsers expose no tracing or probe surfaces.
- Only the Chat timeline scrolls Chat; native `title` attributes are not product help; `reasoning_meta` never leaks into rendering or client persistence; untrusted content renders only through safe text/Markdown/media paths, and attachment paths never become filesystem addressing.
- Production builds minify with **Terser**, not Vite's default esbuild: re-minifying xterm 6 corrupts its DEC-mode query handler into a runtime ReferenceError - changing the minifier requires a real built-browser TUI check, not just Vitest.
- **No xterm WebGL addon:** at fractional desktop scale its canvas sits one pixel off the cell grid painting white row gaps nothing fully rules out; the DOM renderer is the only path. The historical white lines had a second independent cause fixed server-side (`tools/terminal.md`). Do not reintroduce WebGL or a client-side output rewriter without addressing both - live-check striped TUIs at real desktop scale.

## References

Read only when your task matches:

- Finding the owning file/component/controller/style/test -> `webui/source-map.md`
- App startup, navigation, connection/reconnect, global events, invalidation -> `webui/app-shell.md`
- Chat selection/Sessions/history/streaming/Queue/timeline/composer/attachments/speech -> `webui/chat.md`
- Settings, Providers, Extensions, Agents, onboarding, appearance, logs, channels, Desktop Voice -> `webui/settings.md`
- Projects management view/discovery/scans/Team rows/overrides -> `webui/projects.md`
