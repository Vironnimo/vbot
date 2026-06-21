// Small `.svelte.js` helper used by ChatView tests to mirror the parent
// App's reactive `sharedSelectedAgentId` flow. The parent component updates
// the selected id through `onAgentSelected`; that update must round-trip
// back as the `sharedSelectedAgentId` prop so the agent-sync effect in
// ChatView observes the new value.
//
// It also exposes reactive `queueInvalidation` and `sessionsRefreshToken`
// mirrors so the reload-on-change tests can push a fresh `resource_changed`
// signal down as a prop and observe ChatView reacting (queue re-sync, session
// drawer reload).
//
// The reactive `selectedProjectId` + `selectedProjectAgentId` mirrors let the
// project tests model App's flow: a dropdown switch updates `selectedProjectId`
// through `onProjectSelected`, and ChatView reports the active project agent
// back through `onProjectAgentSelected` (the value App would persist).
export function createChatViewParentHarness() {
  let selectedAgentId = $state('alpha');
  let queueInvalidation = $state(null);
  let sessionsRefreshToken = $state(0);
  let selectedProjectId = $state('');
  let selectedProjectAgentId = $state(null);
  return {
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
