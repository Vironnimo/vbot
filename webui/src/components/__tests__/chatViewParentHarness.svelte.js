// Small `.svelte.js` helper used by ChatView tests to mirror the parent
// App's reactive `sharedSelectedAgentId` flow. The parent component updates
// the selected id through `onAgentSelected`; that update must round-trip
// back as the `sharedSelectedAgentId` prop so the agent-sync effect in
// ChatView observes the new value.
//
// It also exposes reactive `queueInvalidation`, `sessionsRefreshToken` and
// `sessionInvalidations` mirrors so the reload-on-change tests can push a
// fresh `resource_changed` signal down as a prop and observe ChatView reacting
// (queue re-sync, session drawer reload).
//
// The reactive `selectedProjectId` + `selectedProjectAgentId` mirrors let the
// project tests model App's flow: a dropdown switch updates `selectedProjectId`
// through `onProjectSelected`, and ChatView reports the active project agent
// back through `onProjectAgentSelected` (the value App would persist).
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
  return {
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
  };
}
