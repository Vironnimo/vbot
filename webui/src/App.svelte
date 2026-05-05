<script>
  import AppShell from './components/AppShell.svelte';
  import AgentsView from './components/AgentsView.svelte';
  import ChatView from './components/ChatView.svelte';
  import ComponentsView from './components/ComponentsView.svelte';
  import SettingsView from './components/SettingsView.svelte';
  import SystemPromptView from './components/SystemPromptView.svelte';
  import './styles/app.css';

  const navigationItems = [
    {
      id: 'chat',
      labelKey: 'navigation.chat',
      labelFallback: 'Chat',
      descriptionKey: 'placeholders.chat.description',
      descriptionFallback:
        'Agent chat will appear here once the chat view is wired.',
    },
    {
      id: 'agents',
      labelKey: 'navigation.agents',
      labelFallback: 'Agents',
      descriptionKey: 'placeholders.agents.description',
      descriptionFallback:
        'Agent creation, editing, and deletion controls are coming next.',
    },
    {
      id: 'system-prompt',
      labelKey: 'navigation.systemPrompt',
      labelFallback: 'System Prompt',
      descriptionKey: 'placeholders.systemPrompt.description',
      descriptionFallback:
        'Editable prompt pieces will be managed from this space later.',
    },
    {
      id: 'settings',
      labelKey: 'navigation.settings',
      labelFallback: 'Settings',
      descriptionKey: 'placeholders.settings.description',
      descriptionFallback:
        'Runtime and WebUI settings placeholders live here for now.',
    },
    {
      id: 'components',
      labelKey: 'navigation.components',
      labelFallback: 'Components',
      descriptionKey: 'components.description',
      descriptionFallback:
        'All defined UI primitives. Click, hover, and interact with each element.',
    },
  ];

  let activeViewId = $state(navigationItems[0].id);
  let agents = $state([]);
  let selectedAgentId = $state('');
  let agentsRefreshToken = $state(0);

  const selectView = (viewId) => {
    activeViewId = viewId;
  };

  const syncAgents = (nextAgents = []) => {
    agents = Array.isArray(nextAgents) ? nextAgents : [];
    if (
      selectedAgentId &&
      !agents.some((agent) => agent.id === selectedAgentId)
    ) {
      selectedAgentId = agents[0]?.id ?? '';
      return;
    }
    if (!selectedAgentId && agents.length > 0) {
      selectedAgentId = agents[0].id;
    }
  };

  const selectAgent = (agentOrId) => {
    selectedAgentId =
      typeof agentOrId === 'string' ? agentOrId : (agentOrId?.id ?? '');
  };

  const refreshAgents = (nextAgents = []) => {
    syncAgents(nextAgents);
    agentsRefreshToken += 1;
  };
</script>

<AppShell items={navigationItems} {activeViewId} onSelectView={selectView}>
  {#if activeViewId === 'chat'}
    <ChatView
      sharedAgents={agents}
      sharedSelectedAgentId={selectedAgentId}
      {agentsRefreshToken}
      onAgentsChanged={syncAgents}
      onAgentSelected={selectAgent}
    />
  {:else if activeViewId === 'agents'}
    <AgentsView
      sharedSelectedAgentId={selectedAgentId}
      onAgentsChanged={refreshAgents}
      onAgentSelected={selectAgent}
    />
  {:else if activeViewId === 'system-prompt'}
    <SystemPromptView />
  {:else if activeViewId === 'settings'}
    <SettingsView />
  {:else if activeViewId === 'components'}
    <ComponentsView />
  {/if}
</AppShell>
