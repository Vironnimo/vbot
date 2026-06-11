// Browser-history integration for the app shell. Tab switches and chat
// session overrides become `history.pushState` entries so Back/Forward
// navigate inside the SPA (e.g. from a sub-agent session back to the parent)
// instead of leaving the app. App.svelte owns the push/popstate wiring; these
// helpers keep the state format and comparisons unit-testable.

const NAVIGATION_STATE_MARKER = 'vbot.navigation';

export const createNavigationHistoryState = (
  viewId,
  sessionOverride = null,
) => ({
  marker: NAVIGATION_STATE_MARKER,
  view: viewId,
  session: sessionOverride
    ? {
        agentId: sessionOverride.agentId ?? '',
        sessionId: sessionOverride.sessionId ?? '',
        subAgent: sessionOverride.subAgent === true,
      }
    : null,
});

export const isNavigationHistoryState = (value) =>
  Boolean(value) &&
  value.marker === NAVIGATION_STATE_MARKER &&
  typeof value.view === 'string' &&
  value.view !== '';

export const sameSessionOverride = (left, right) => {
  if (!left && !right) {
    return true;
  }
  if (!left || !right) {
    return false;
  }
  return (
    left.agentId === right.agentId &&
    left.sessionId === right.sessionId &&
    (left.subAgent === true) === (right.subAgent === true)
  );
};

export const viewIdFromLocationHash = (hash, knownViewIds) => {
  const normalized = String(hash ?? '').replace(/^#\/?/, '');
  return knownViewIds.includes(normalized) ? normalized : '';
};

export const locationHashForView = (viewId) => `#${viewId}`;
