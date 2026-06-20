// Generic "resource X changed → reload it" client plumbing.
//
// The server's `resource_changed` event names only a *kind* of shared app state
// that changed, never the new data. This module owns two pure halves so they
// stay unit-testable and framework-free:
//   (a) routing a kind to the refresh-token group(s) it invalidates, and
//   (b) the "apply the visible swap now, or defer it" decision per surface type.
// App.svelte holds the reactive refresh-token state; each view holds its own
// reactive editing signals. Neither half touches Svelte.

// Refresh-token groups a surface can watch. A surface reloads when its token
// bumps. The `queue` kind is deliberately NOT a token group: it carries a
// session scope the watcher must match, so App routes it directly rather than
// through a bare counter.
export const RESOURCE_TOKEN_MODELS = 'models';
export const RESOURCE_TOKEN_AGENTS = 'agents';
export const RESOURCE_TOKEN_SESSIONS = 'sessions';
export const RESOURCE_TOKEN_CLIENTS = 'clients';

// Which token group(s) each resource kind invalidates. Both a model-catalog
// refresh ("models") and a provider/credential change ("providers") alter which
// models are selectable, so both bump the "models" group — a consuming surface
// reloads its model list AND its connection list together. Agent CRUD bumps the
// "agents" group (re-fetch agent.list) and session create/switch bumps the
// "sessions" group (re-fetch a session list). New kinds add their mapping here.
const KIND_TOKEN_GROUPS = {
  models: [RESOURCE_TOKEN_MODELS],
  providers: [RESOURCE_TOKEN_MODELS],
  agents: [RESOURCE_TOKEN_AGENTS],
  sessions: [RESOURCE_TOKEN_SESSIONS],
  clients: [RESOURCE_TOKEN_CLIENTS],
};

// Return the refresh-token group(s) a resource kind invalidates (empty for an
// unknown or out-of-scope kind, so the caller bumps nothing).
export function tokenKeysForKind(kind) {
  return KIND_TOKEN_GROUPS[kind] ?? [];
}

// Surface types for the apply/defer decision.
export const SURFACE_DISPLAY = 'display';
export const SURFACE_FORM = 'form';

// A form/picker is "busy" — actively being edited — while a dropdown is open, a
// field holds focus, or a debounced save is still pending. While busy, a reload
// must hold its visible swap so it cannot yank an open selection or half-typed
// input out from under the user.
export function isSurfaceBusy(signals = {}) {
  const {
    dropdownOpen = false,
    focused = false,
    savePending = false,
  } = signals;
  return Boolean(dropdownOpen || focused || savePending);
}

// Decide whether a freshly-arrived reload may swap visible data now. Pure
// displays always apply immediately; forms/pickers apply only when not busy.
// This gates only the *visible* swap — callers still fetch fresh data right away
// and apply the deferred swap once the surface goes idle.
export function shouldApplyReloadNow(surface, signals = {}) {
  if (surface === SURFACE_DISPLAY) {
    return true;
  }
  return !isSurfaceBusy(signals);
}
