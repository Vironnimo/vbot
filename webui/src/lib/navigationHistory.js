// Browser-history integration for the app shell. Tab switches and chat
// session overrides become `history.pushState` entries so Back/Forward
// navigate inside the SPA (e.g. from a sub-agent session back to the parent)
// instead of leaving the app. App.svelte owns the push/popstate wiring; these
// helpers keep the state format and comparisons unit-testable.

const NAVIGATION_STATE_MARKER = 'vbot.navigation';

// A history entry captures the whole chat context that was on screen when it
// was created: the session override (drawer pick / sub-agent view, or null for
// the current-session view) AND the selection — selected identity agent plus
// the project context (chosen project and active project agent). Restoring an
// entry re-establishes all of it, so Back never shows one agent's session
// under another agent's chips.
export const createNavigationHistoryState = (
  viewId,
  sessionOverride = null,
  selection = null,
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
  selection: selection
    ? {
        agentId: selection.agentId ?? '',
        projectId: selection.projectId ?? '',
        // Tri-state like App's persisted mirror: null = nothing remembered,
        // '' = an identity agent active alongside the project, id = member.
        projectAgentId:
          typeof selection.projectAgentId === 'string'
            ? selection.projectAgentId
            : null,
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

// Whether two selection snapshots describe the same chat context. Two empty
// selections (entries from before the selection field existed, or foreign
// states) compare equal; an empty vs. a set one differs — the restore then
// decides what to apply.
export const sameNavigationSelection = (left, right) => {
  if (!left && !right) {
    return true;
  }
  if (!left || !right) {
    return false;
  }
  return (
    (left.agentId ?? '') === (right.agentId ?? '') &&
    (left.projectId ?? '') === (right.projectId ?? '') &&
    (left.projectAgentId ?? null) === (right.projectAgentId ?? null)
  );
};

// The view a location hash names, whether or not that view is known yet.
export const requestedViewIdFromLocationHash = (hash) =>
  String(hash ?? '').replace(/^#\/?/, '');

export const viewIdFromLocationHash = (hash, knownViewIds) => {
  const normalized = requestedViewIdFromLocationHash(hash);
  return knownViewIds.includes(normalized) ? normalized : '';
};

// Extension page routes join the known views only once the server's page
// catalog has loaded, after startup.
export const isExtensionViewId = (viewId) =>
  typeof viewId === 'string' && viewId.startsWith('extension:');

export const locationHashForView = (viewId) => `#${viewId}`;

const OPEN_AGENT_PARAM = 'open_agent';
const OPEN_SESSION_PARAM = 'open_session';

// A link that opens one Session when the app loads:
// `?open_agent=<Agent address>&open_session=<Session id>` (the Desktop app and
// the tray build these). Returns null when the search string has neither
// parameter; otherwise the Session (null unless both are non-empty) and the
// search string without the two parameters, other parameters keeping their
// exact spelling.
export function sessionLinkFromSearch(search) {
  let agentId = '';
  let sessionId = '';
  let found = false;
  const kept = String(search ?? '')
    .replace(/^\?/, '')
    .split('&')
    .filter((pair) => {
      if (!pair) return false;
      const [[name, value] = []] = new URLSearchParams(pair);
      if (name === OPEN_AGENT_PARAM) agentId = value;
      else if (name === OPEN_SESSION_PARAM) sessionId = value;
      else return true;
      found = true;
      return false;
    });
  if (!found) return null;
  return {
    target: agentId && sessionId ? { agentId, sessionId } : null,
    search: kept.length ? `?${kept.join('&')}` : '',
  };
}
