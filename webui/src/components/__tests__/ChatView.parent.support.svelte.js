// Mirrors the reactive props App passes to ChatView (and to the Session
// drawer), so a test can change them after mounting and observe the component
// react. Callbacks write back into the harness the way App does: a selection
// reported through `onAgentSelected` returns as `sharedSelectedAgentId`, a
// dropdown switch through `onProjectSelected` as `selectedProjectId`, and the
// Project Agent ChatView reports through `onProjectAgentSelected` is the value
// App would persist.
//
// `connectionSnapshot` is `$state.raw`: ChatView applies each snapshot object
// once and compares by reference, which a deep proxy would break.
import { appendSessionInvalidation } from '../../lib/sessionInvalidation.js';

export function createChatViewParentHarness() {
  let selectedAgentId = $state('alpha');
  let queueInvalidation = $state(null);
  let sessionsRefreshToken = $state(0);
  let sessionInvalidations = $state([]);
  let sessionInvalidationId = 0;
  let sessionListActivity = $state([]);
  let agentsRefreshToken = $state(0);
  let pendingSessionNavigation = $state(null);
  let selectedProjectId = $state('');
  let selectedProjectAgentId = $state(null);
  let connectionSnapshot = $state.raw(null);

  // ChatView props per group of App state: live values and the callbacks
  // that write back into the harness.
  const bindings = {
    agent: {
      live: { sharedSelectedAgentId: () => selectedAgentId },
      callbacks: { onAgentSelected: (agentId) => (selectedAgentId = agentId) },
    },
    project: {
      live: {
        selectedProjectId: () => selectedProjectId,
        sharedSelectedProjectAgentId: () => selectedProjectAgentId,
      },
      callbacks: {
        onProjectSelected: (projectId) => (selectedProjectId = projectId),
        onProjectAgentSelected: (agentId) => (selectedProjectAgentId = agentId),
      },
    },
    navigation: {
      live: { pendingSessionNavigation: () => pendingSessionNavigation },
    },
    sessions: {
      live: {
        sessionsRefreshToken: () => sessionsRefreshToken,
        sessionInvalidations: () => sessionInvalidations,
      },
    },
    queue: { live: { queueInvalidation: () => queueInvalidation } },
    agents: { live: { agentsRefreshToken: () => agentsRefreshToken } },
    connection: { live: { connectionSnapshot: () => connectionSnapshot } },
  };

  return {
    // ChatView props bound to `groups` of App state, on top of the static
    // `props`.
    props(groups, props = {}) {
      const bound = { ...props };
      for (const group of groups) {
        const { live, callbacks = {} } = bindings[group];
        for (const [name, read] of Object.entries(live)) {
          Object.defineProperty(bound, name, { get: read, enumerable: true });
        }
        Object.assign(bound, callbacks);
      }
      return bound;
    },
    get agentsRefreshToken() {
      return agentsRefreshToken;
    },
    bumpAgentsRefreshToken() {
      agentsRefreshToken += 1;
    },
    get pendingSessionNavigation() {
      return pendingSessionNavigation;
    },
    setPendingSessionNavigation(navigation) {
      pendingSessionNavigation = navigation;
    },
    get selectedAgentId() {
      return selectedAgentId;
    },
    setSelectedAgentId(agentId) {
      selectedAgentId = agentId;
    },
    get queueInvalidation() {
      return queueInvalidation;
    },
    setQueueInvalidation(scope) {
      queueInvalidation = scope;
    },
    get sessionsRefreshToken() {
      return sessionsRefreshToken;
    },
    bumpSessionsRefreshToken() {
      sessionsRefreshToken += 1;
    },
    get sessionInvalidations() {
      return sessionInvalidations;
    },
    pushSessionInvalidation(scope) {
      sessionInvalidationId += 1;
      sessionInvalidations = appendSessionInvalidation(
        sessionInvalidations,
        sessionInvalidationId,
        scope,
      );
    },
    get sessionListActivity() {
      return sessionListActivity;
    },
    setSessionListActivity(activity) {
      sessionListActivity = activity;
    },
    get selectedProjectId() {
      return selectedProjectId;
    },
    setSelectedProjectId(projectId) {
      selectedProjectId = projectId;
    },
    get selectedProjectAgentId() {
      return selectedProjectAgentId;
    },
    setSelectedProjectAgentId(agentId) {
      selectedProjectAgentId = agentId;
    },
    get connectionSnapshot() {
      return connectionSnapshot;
    },
    setConnectionSnapshot(snapshot) {
      connectionSnapshot = snapshot;
    },
  };
}
