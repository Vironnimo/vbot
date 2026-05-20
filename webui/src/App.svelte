<script module>
  export const NAVIGATION_ITEMS = Object.freeze([
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
      id: 'cron',
      labelKey: 'navigation.cron',
      labelFallback: 'Cron',
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
      id: 'logs',
      labelKey: 'navigation.logs',
      labelFallback: 'Logs',
    },
  ]);
</script>

<script>
  import { onMount } from 'svelte';
  import AppShell from './components/AppShell.svelte';
  import AgentsView from './components/AgentsView.svelte';
  import ChatView from './components/ChatView.svelte';
  import CronView from './components/CronView.svelte';
  import LogsView from './components/LogsView.svelte';
  import SettingsView from './components/SettingsView.svelte';
  import SystemPromptView from './components/SystemPromptView.svelte';
  import ToastStack from './components/ToastStack.svelte';
  import {
    createConnectionState,
    connect,
    disconnect,
  } from '$lib/connectionState.js';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { createToastState, addToast, dismissToast } from '$lib/toastState.js';
  import './styles/app.css';

  const navigationItems = NAVIGATION_ITEMS;
  const SELECTED_AGENT_KEY = 'vbot.selectedAgentId';

  let activeViewId = $state(navigationItems[0].id);
  let agents = $state([]);
  let selectedAgentId = $state(
    (typeof localStorage !== 'undefined' &&
      localStorage.getItem(SELECTED_AGENT_KEY)) ||
      '',
  );
  let agentsRefreshToken = $state(0);
  let connectionState = $state(createConnectionState());
  let toastState = $state(createToastState());
  let pendingSubAgentNavigation = $state(null);
  let providerAuthEvent = $state(null);

  $effect(() => {
    try {
      if (selectedAgentId) {
        localStorage.setItem(SELECTED_AGENT_KEY, selectedAgentId);
      } else {
        localStorage.removeItem(SELECTED_AGENT_KEY);
      }
    } catch {
      // localStorage unavailable (private browsing, storage quota)
    }
  });

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

  const navigateToSubAgent = (targetOrAgentId, maybeSessionId) => {
    const agentId =
      typeof targetOrAgentId === 'string'
        ? targetOrAgentId
        : (targetOrAgentId?.agentId ?? '');
    const sessionId =
      typeof targetOrAgentId === 'string'
        ? maybeSessionId
        : targetOrAgentId?.sessionId;

    if (!agentId || !sessionId) {
      return;
    }

    selectView('chat');
    selectAgent(agentId);
    pendingSubAgentNavigation = { agentId, sessionId };
  };

  const refreshAgents = (nextAgents = []) => {
    syncAgents(nextAgents);
    agentsRefreshToken += 1;
  };

  const handleServerEvent = async (event) => {
    if (event.type === 'app_error') {
      addToast(toastState, {
        title: t('errors.appError', 'Error'),
        message: event.payload?.message ?? '',
        variant: 'error',
      });
      return;
    }

    if (event.type === 'provider_auth_completed') {
      providerAuthEvent = event;
      return;
    }

    const agentEventTypes = ['agent.created', 'agent.updated', 'agent.deleted'];
    if (!agentEventTypes.includes(event.type)) {
      return;
    }
    try {
      const result = await rpc('agent.list');
      refreshAgents(result.agents);
    } catch (error) {
      console.warn('Agent list refresh failed:', error);
    }
  };

  onMount(() => {
    connect(connectionState, { onEvent: handleServerEvent });
    return () => disconnect(connectionState);
  });
</script>

<AppShell
  items={navigationItems}
  {activeViewId}
  onSelectView={selectView}
  connectionStatus={connectionState.status}
>
  {#if activeViewId === 'chat'}
    <ChatView
      sharedAgents={agents}
      sharedSelectedAgentId={selectedAgentId}
      {agentsRefreshToken}
      onAgentsChanged={syncAgents}
      onAgentSelected={selectAgent}
      {navigateToSubAgent}
      {pendingSubAgentNavigation}
    />
  {:else if activeViewId === 'agents'}
    <AgentsView
      sharedSelectedAgentId={selectedAgentId}
      onAgentsChanged={refreshAgents}
      onAgentSelected={selectAgent}
    />
  {:else if activeViewId === 'cron'}
    <CronView />
  {:else if activeViewId === 'system-prompt'}
    <SystemPromptView />
  {:else if activeViewId === 'settings'}
    <SettingsView {providerAuthEvent} />
  {:else if activeViewId === 'logs'}
    <LogsView />
  {/if}
  <ToastStack
    toasts={toastState.toasts}
    onDismiss={(id) => dismissToast(toastState, id)}
  />
</AppShell>
