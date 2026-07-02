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

export const viewIdFromLocationHash = (hash, knownViewIds) => {
  const normalized = String(hash ?? '').replace(/^#\/?/, '');
  return knownViewIds.includes(normalized) ? normalized : '';
};

export const locationHashForView = (viewId) => `#${viewId}`;
