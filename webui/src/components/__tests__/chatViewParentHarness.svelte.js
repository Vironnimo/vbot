// Small `.svelte.js` helper used by ChatView tests to mirror the parent
// App's reactive `sharedSelectedAgentId` flow. The parent component updates
// the selected id through `onAgentSelected`; that update must round-trip
// back as the `sharedSelectedAgentId` prop so the agent-sync effect in
// ChatView observes the new value.
export function createChatViewParentHarness() {
  let selectedAgentId = $state('alpha');
  return {
    get selectedAgentId() {
      return selectedAgentId;
    },
    setSelectedAgentId(agentId) {
      selectedAgentId = agentId;
    },
  };
}
