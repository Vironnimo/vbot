# WebUI App Shell

Read this reference only for WebUI application-shell, navigation, connectivity, server-event, or global invalidation work. The root boundary and domain-wide frontend invariants live in `webui.md`; file-level ownership lives in `webui/source-map.md`.

## Ownership

`App.svelte` composes the major views and creates the long-lived application controller. `appController.js` owns global loading, active-view availability, navigation, server-event dispatch, and refresh coordination. `api.js` owns the actual HTTP, WebSocket, and SSE transport adapters; `connectionState.js`, `navigationHistory.js`, and `resourceInvalidation.js` keep their respective state machines out of the root component.

Domain controllers still own their data. The app shell may request a refresh or route a lifecycle event, but it must not duplicate Chat, Provider, Extension, Project, or Settings rules.

## Startup and availability

- Startup loads the server-backed resources required to decide which views are available, then mounts view state through `createAppController`. Failure to load one optional surface must not turn the whole accessor into a second source of truth.
- View availability is derived from current capabilities and server state. If the active view becomes unavailable, navigation resolves to a valid view instead of leaving an unreachable route selected.
- Browser history is coordinated through `navigationHistory.js`; forward/back navigation changes the active view without inventing a separate route state inside each view.
- Onboarding is a Settings-owned flow that the app shell can open based on operational readiness. Its detailed policy belongs in `webui/settings.md`.

## Server connection and events

- `subscribeServerEvents()` opens the app-wide WebSocket. A `connection_ready` payload establishes the server epoch and replay position; reconnect logic distinguishes a transport interruption from a server restart.
- Reconnect uses bounded backoff and exposes connection state to the shell. A short interruption is tolerated before the offline notice is shown so transient browser/network churn does not flash disruptive UI.
- `connection_ready` replaces the WebSocket epoch and replay cursor, so a restarted server can resume at sequence 1 without the browser dropping valid events. Its `active_runs` snapshot is forwarded to Chat for Run reconciliation; it is not a generic replacement for every domain controller's data.
- Recovery after a confirmed offline notice increments `serverRecoveryGeneration`, remounting the active main surface so its normal load path refreshes server-backed state. A reconnect that completes inside the notice grace period does not force that remount.
- WebSocket Run events are lifecycle summaries used for global awareness and recovery. Per-Run output, reasoning, tool-call, and log deltas arrive through the Run SSE subscription owned by Chat.
- Presence is server-owned. `clients.list` supplies the Settings projection; the WebSocket only signals when that projection should refresh.

## Resource invalidation

- `resource_changed` events carry a resource family and optional scope. `resourceInvalidation.js` converts them into refresh tokens or targeted callbacks rather than copying RPC-specific branching into every component.
- Queue invalidation is scoped to the addressed Agent and Session. Other resource families refresh the controller that owns their displayed projection.
- Refresh completion does not automatically replace an active editing surface. A controller can retain the visible snapshot while a modal, picker, or draft is busy, then adopt the newest server result at the safe boundary.
- Event handlers must be idempotent because reconnect replay and explicit refreshes can describe state the browser already knows.

## Transport contracts

- RPC requests go through the wrappers in `api.js`; validation, network, HTTP, malformed-envelope, malformed-SSE, and malformed-WebSocket failures are normalized before reaching views.
- `subscribeLogEvents()` owns the per-file log WebSocket. Log browsing behavior lives with the Settings/log surfaces; it does not share Run SSE semantics.
- Attachment downloads use server URLs returned by `getAttachmentUrl()` and remain outside JSON-RPC.
- Debug wrappers remain guarded by the frontend development gate and the server's debug policy.

## Source and tests

- Composition and lifecycle: `webui/src/App.svelte`, `webui/src/lib/appController.js`
- Connection and replay state: `webui/src/lib/connectionState.js`, `webui/src/lib/api.js`
- Navigation and invalidation: `webui/src/lib/navigationHistory.js`, `webui/src/lib/resourceInvalidation.js`
- Focused coverage: `webui/src/lib/__tests__/api.test.js`, `appController.test.js`, `connectionState.test.js`, `navigationHistory.test.js`, `resourceInvalidation.test.js`, plus `webui/src/__tests__/App*.test.js`
