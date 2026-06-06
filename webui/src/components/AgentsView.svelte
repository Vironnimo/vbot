<script>
  import { onMount } from 'svelte';

  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

  import AgentCreateModal from './agents/AgentCreateModal.svelte';
  import AgentEditor from './agents/AgentEditor.svelte';
  import AgentListPane from './agents/AgentListPane.svelte';

  const noop = () => {};

  let {
    sharedSelectedAgentId = '',
    onAgentsChanged,
    onAgentSelected,
    onToast = noop,
  } = $props();

  let agents = $state([]);
  let selectedAgentId = $state('');
  let lastSharedSelectedAgentId = $state('');
  let isCreateModalOpen = $state(false);
  let isLoading = $state(false);
  let loadError = $state('');
  let availableModels = $state([]);
  let availableConnections = $state([]);
  let availableTools = $state([]);
  let availableSkills = $state([]);
  let invalidSkills = $state([]);

  let selectedAgent = $derived(
    agents.find((agent) => agent.id === selectedAgentId) ?? null,
  );

  $effect(() => {
    if (
      sharedSelectedAgentId &&
      sharedSelectedAgentId !== lastSharedSelectedAgentId &&
      agents.some((agent) => agent.id === sharedSelectedAgentId)
    ) {
      lastSharedSelectedAgentId = sharedSelectedAgentId;
      if (sharedSelectedAgentId !== selectedAgentId) {
        selectAgent(sharedSelectedAgentId);
      }
    } else if (!sharedSelectedAgentId) {
      lastSharedSelectedAgentId = sharedSelectedAgentId;
    }
  });

  onMount(() => {
    void loadCatalogs();
    void loadAgents({ preferredAgentId: sharedSelectedAgentId });
  });

  async function loadCatalogs() {
    try {
      const [modelsResult, connectionsResult, toolsResult, skillsResult] =
        await Promise.all([
          rpc('model.list'),
          rpc('connection.list'),
          rpc('tool.list'),
          rpc('skill.list'),
        ]);

      availableModels = Array.isArray(modelsResult?.models)
        ? modelsResult.models
        : [];
      availableConnections = Array.isArray(connectionsResult?.connections)
        ? connectionsResult.connections
        : [];
      availableTools = Array.isArray(toolsResult?.tools)
        ? toolsResult.tools
        : [];
      availableSkills = Array.isArray(skillsResult?.skills)
        ? skillsResult.skills
        : [];
      invalidSkills = Array.isArray(skillsResult?.invalid_skills)
        ? skillsResult.invalid_skills
        : [];
    } catch (error) {
      loadError = viewErrorMessage(error, t('agents.loadError'));
    }
  }

  async function loadAgents(options = {}) {
    isLoading = true;
    loadError = '';

    try {
      const result = await rpc('agent.list');
      agents = Array.isArray(result?.agents) ? result.agents : [];
      const preferredAgentId = options.preferredAgentId ?? selectedAgentId;
      selectAgent(resolveSelectedAgentId(agents, preferredAgentId));
      notifyAgentsChanged();
    } catch (error) {
      loadError = viewErrorMessage(error, t('agents.loadError'));
    } finally {
      isLoading = false;
    }
  }

  function resolveSelectedAgentId(nextAgents, preferredAgentId) {
    if (nextAgents.some((agent) => agent.id === preferredAgentId)) {
      return preferredAgentId;
    }

    return nextAgents[0]?.id ?? '';
  }

  function selectAgent(agentId) {
    selectedAgentId = agentId;
    const agent = agents.find((item) => item.id === agentId) ?? null;
    if (agent) {
      onAgentSelected?.(agent);
    }
  }

  function handleAgentUpdated(nextAgent, options = {}) {
    agents = agents.map((agent) =>
      agent.id === nextAgent.id ? nextAgent : agent,
    );
    notifyAgentsChanged();

    if (options.notifySelection !== false && selectedAgentId === nextAgent.id) {
      onAgentSelected?.(nextAgent);
    }
  }

  async function handleAgentCreated(agentId) {
    isCreateModalOpen = false;
    await loadAgents({ preferredAgentId: agentId });
  }

  async function handleAgentDeleted() {
    await loadAgents();
  }

  function notifyAgentsChanged() {
    onAgentsChanged?.(agents);
  }

  function viewErrorMessage(error, fallback) {
    return (
      error?.message ||
      fallback ||
      t('errors.generic', 'Something went wrong. Try again.')
    );
  }
</script>

<section class="agents-view view active" aria-labelledby="agents-list-title">
  <div class="agents-layout">
    <AgentListPane
      {agents}
      {selectedAgentId}
      {isLoading}
      onSelect={selectAgent}
      onCreate={() => {
        isCreateModalOpen = true;
      }}
    />

    {#key selectedAgent?.id ?? 'new-agent'}
      <AgentEditor
        agent={selectedAgent}
        agentsCount={agents.length}
        {availableModels}
        {availableConnections}
        {availableTools}
        {availableSkills}
        {invalidSkills}
        {loadError}
        onAgentUpdated={handleAgentUpdated}
        onAgentCreated={handleAgentCreated}
        onAgentDeleted={handleAgentDeleted}
        {onToast}
      />
    {/key}
  </div>

  {#if isCreateModalOpen}
    <AgentCreateModal
      {availableModels}
      {availableConnections}
      onCreated={handleAgentCreated}
      onClose={() => {
        isCreateModalOpen = false;
      }}
      {onToast}
    />
  {/if}
</section>
