# WebUI App Shell

Read this reference only for WebUI application-shell, navigation, connectivity, server-event, or global invalidation work. The root boundary and domain-wide frontend invariants live in `webui.md`; file-level ownership lives in `webui/source-map.md`.

## Ownership

`App.svelte` composes the major views and creates the long-lived application controller. `appController.js` owns global loading, active-view availability, navigation, server-event dispatch, and refresh coordination. `AppShell.svelte` owns global viewport/focus presentation, including the capability-gated Desktop context menu. `api.js` owns the actual HTTP, WebSocket, and SSE transport adapters; `connectionState.js`, `navigationHistory.js`, and `resourceInvalidation.js` keep their respective state machines out of the root component.

Domain controllers still own their data. The app shell may request a refresh or route a lifecycle event, but it must not duplicate Chat, Provider, Extension, Project, or Settings rules.

## Desktop context menu

- `App.svelte` enables the custom menu only after the live Desktop bridge advertises `contextMenu`; an ordinary browser never has its native `contextmenu` event cancelled.
- `AppShell.svelte` derives actions from the event's composed DOM path: safe absolute HTTP(S) links expose Copy link address and Open in browser, selected non-sensitive text exposes Copy, and writable text controls additionally expose Cut and Paste. Password controls expose Paste without copying/cutting their selected value. It snapshots the selection before moving focus into the menu so editing actions still target the original control.
- The menu measures after render, clamps to the viewport, focuses its first action, supports Arrow Up/Down, Home/End, and Escape, and closes on outside press, captured ancestor scroll, resize, or window blur. Action completion restores the original focus target.
- Native operations go through `lib/desktopBridge.js`; AppShell never depends on `navigator.clipboard`, and the Desktop Python boundary repeats URL validation. Failures produce one warning Toast through App's existing `onToast` callback.

## Startup and availability

- Startup loads the server-backed resources required to decide which views are available, then mounts view state through `createAppController`. Project-catalog loads are generation-guarded so only the newest outstanding response updates the shell, and a transient failure retains the last valid catalog. Failure to load one optional surface must not turn the whole accessor into a second source of truth.
- View availability is derived from current capabilities and server state. If the active view becomes unavailable, navigation resolves to a valid view instead of leaving an unreachable route selected.
- User-initiated context changes that would replace an autosave editor-main-view navigation, Agent or Project selection, System Prompt scope selection, and editor deep links-go through the coordinator in `autosave.js`. Registered editors cancel their debounce, wait for any in-flight save, and keep saving newer snapshots until stable; the requested transition runs only after every pending participant succeeds. A failure leaves the editor mounted and opens the App-owned Retry / Discard and continue modal. Browser tab visibility is outside this contract, and explicit confirmation or special-operation flows do not become autosave operations.
- Browser history is coordinated through `navigationHistory.js`; forward/back navigation changes the active view without inventing a separate route state inside each view.
- Within one `serverRecoveryGeneration`, `App.svelte` keeps a single Chat surface and its DOM mounted even while another main view is active; ordinary navigation changes its `active` visibility instead of replacing it. This preserves Chat-owned selection, drafts, timeline viewport/disclosures, transient state, and Run-stream ownership, and prevents roster/History/activity initialization from repeating when the user returns.
- Onboarding is a Settings-owned flow that the app shell can open based on operational readiness. Its detailed policy belongs in `webui/settings.md`.

## Server connection and events

- `subscribeServerEvents()` opens the app-wide WebSocket. A `connection_ready` payload establishes the server epoch, replay position, and replay completeness; reconnect logic distinguishes a fresh connection, complete resume, replay gap, and server restart.
- Reconnect uses bounded backoff and exposes connection state to the shell. A short interruption is tolerated before the offline notice is shown so transient browser/network churn does not flash disruptive UI.
- The global offline notice replaces connection-state symptoms but does not dismiss unrelated sticky error toasts; those remain until the user acknowledges them. New error toasts are suppressed while the connection is already known to be offline so dependent failures do not flood the UI.
- The offline notice keeps Retry as the normal browser recovery. When Desktop capability discovery reports `serverSelection`, it also offers Switch server; that action hides the notice and opens the shared Desktop remembered-server picker in a modal rendered outside AppShell's inert main content. Browser accessors never see this action, and launch-time Desktop failures remain with the native Connection screen because the WebUI is not available yet.
- `connection_ready.replay_status` is `fresh`, `resumed`, `gap`, or `epoch_changed`. A complete resume keeps the client's acknowledged cursor until replayed events arrive; a gap or epoch change adopts the hello high-water mark and invalidates every resource-backed projection through its existing owner. The authoritative `active_runs` and public `queues` snapshots are forwarded to Chat for immediate Run and Queue reconciliation.
- Recovery after a confirmed offline notice increments `serverRecoveryGeneration`, remounting the keyed main-view generation-including the retained Chat owner-so normal load paths refresh server-backed state. A reconnect that completes inside the notice grace period does not force that remount.
- WebSocket Run events are lifecycle summaries used for global awareness and recovery. Per-Run output, reasoning, tool-call, and log deltas arrive through the Run SSE subscription owned by Chat.
- Presence is server-owned. `clients.list` supplies the Settings projection; the WebSocket only signals when that projection should refresh.

## Resource invalidation

- `resource_changed` events carry a resource family and optional scope. `resourceInvalidation.js` converts them into refresh tokens or targeted callbacks rather than copying RPC-specific branching into every component.
- `commands` invalidation bumps the App-owned Command refresh token forwarded to Chat. Replay gaps and epoch changes bump it with the other server-backed projections so the active autocomplete catalog is re-fetched after uncertain continuity.
- Queue invalidation is scoped to the addressed Agent and Session. Other resource families refresh the controller that owns their displayed projection.
- `terminals` invalidation bumps the App-owned terminal refresh token. The mounted Terminals controller re-fetches the active catalog without replacing a still-valid selection; PTY output is not an invalidation and remains on the selected terminal's dedicated stream.
- Refresh completion does not automatically replace an active editing surface. A controller can retain the visible snapshot while a modal, picker, or draft is busy, then adopt the newest server result at the safe boundary.
- Event handlers must be idempotent because reconnect replay and explicit refreshes can describe state the browser already knows.

## Transport contracts

- RPC requests go through the wrappers in `api.js`; validation, network, HTTP, malformed-envelope, malformed-SSE, and malformed-WebSocket failures are normalized before reaching views.
- `subscribeLogEvents()` owns the per-file log WebSocket. Log browsing behavior lives with the Settings/log surfaces; it does not share Run SSE semantics.
- `subscribeTerminalEvents()` owns the per-Terminal server-push WebSocket. It carries an authoritative snapshot plus sequenced live output/state only; terminal input, resize, and stop remain RPC wrappers, and the view/controller owns reconnect and cleanup.
- Attachment downloads use server URLs returned by `getAttachmentUrl()` and remain outside JSON-RPC.
- Debug wrappers remain guarded by the frontend development gate and the server's debug policy.

## Source and tests

- Composition and lifecycle: `webui/src/App.svelte`, `webui/src/lib/appController.js`
- Connection and replay state: `webui/src/lib/connectionState.js`, `webui/src/lib/api.js`
- Navigation, autosave coordination, and invalidation: `webui/src/lib/navigationHistory.js`, `webui/src/lib/autosave.js`, `webui/src/lib/resourceInvalidation.js`
- Focused coverage: `webui/src/lib/__tests__/api.test.js`, `appController.test.js`, `connectionState.test.js`, `navigationHistory.test.js`, `resourceInvalidation.test.js`, plus `webui/src/__tests__/App*.test.js`
