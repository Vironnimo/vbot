<script module>
  export const NAVIGATION_ITEMS = Object.freeze([
    {
      id: 'chat',
      labelKey: 'navigation.chat',
      labelFallback: 'Chat',
    },
    {
      id: 'agents',
      labelKey: 'navigation.agents',
      labelFallback: 'Agents',
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
    },
    {
      id: 'settings',
      labelKey: 'navigation.settings',
      labelFallback: 'Settings',
    },
    {
      id: 'logs',
      labelKey: 'navigation.logs',
      labelFallback: 'Logs',
    },
    {
      id: 'debug',
      labelKey: 'navigation.debug',
      labelFallback: 'Debug',
    },
  ]);
</script>

<script>
  import { onMount } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import AppShell from './components/AppShell.svelte';
  import AgentsView from './components/AgentsView.svelte';
  import ChatView from './components/ChatView.svelte';
  import CronView from './components/CronView.svelte';
  import DebugView from './components/DebugView.svelte';
  import LogsView from './components/LogsView.svelte';
  import SettingsView from './components/SettingsView.svelte';
  import SystemPromptView from './components/SystemPromptView.svelte';
  import ToastStack from './components/ToastStack.svelte';
  import {
    createConnectionState,
    connect,
    disconnect,
  } from '$lib/connectionState.js';
  import { rpc, debugStatus } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { createToastState, addToast, dismissToast } from '$lib/toastState.js';
  import {
    isDesktopAccessor,
    getDesktopCapabilities,
    onWakewordStatusChange,
    waitForDesktopBridge,
  } from '$lib/desktopBridge.js';
  import './styles/app.css';

  const navigationItems = NAVIGATION_ITEMS;
  const visibleNavigationItems = $derived(
    debugEnabled
      ? navigationItems
      : navigationItems.filter((item) => item.id !== 'debug'),
  );
  const SELECTED_AGENT_KEY = 'vbot.selectedAgentId';
  const TOAST_AUTO_DISMISS_MS = 3200;
  const MAX_RUN_SERVER_EVENTS = 500;
  const RUN_SERVER_EVENT_TYPES = new Set([
    'run_started',
    'run_output',
    'run_completed',
    'run_cancelled',
    'run_failed',
  ]);

  const readStoredSelectedAgentId = () => {
    try {
      if (typeof localStorage === 'undefined') {
        return '';
      }
      return localStorage.getItem(SELECTED_AGENT_KEY) || '';
    } catch {
      return '';
    }
  };

  let activeViewId = $state(navigationItems[0].id);
  let debugEnabled = $state(false);
  let agents = $state([]);
  let selectedAgentId = $state(readStoredSelectedAgentId());
  let agentsRefreshToken = $state(0);
  let connectionState = $state(createConnectionState());
  let toastState = $state(createToastState());
  let pendingSubAgentNavigation = $state(null);
  let providerAuthEvent = $state(null);
  let runServerEvents = $state([]);
  let desktopCapabilities = $state(null);
  let wakewordStatus = $state({ enabled: false, state: 'off' });
  let settingsPanelTarget = $state('');
  let settingsPanelTargetRequestId = $state(0);
  let subAgentNavigationRequestId = 0;
  let cleanupWakewordPoll = null;
  const toastDismissTimers = new SvelteMap();

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
    subAgentNavigationRequestId += 1;
    pendingSubAgentNavigation = {
      agentId,
      sessionId,
      requestId: subAgentNavigationRequestId,
    };
  };

  const refreshAgents = (nextAgents = []) => {
    syncAgents(nextAgents);
    agentsRefreshToken += 1;
  };

  const clearToastDismissTimer = (id) => {
    const timer = toastDismissTimers.get(id);
    if (!timer) {
      return;
    }

    clearTimeout(timer);
    toastDismissTimers.delete(id);
  };

  const clearToastDismissTimers = () => {
    for (const timer of toastDismissTimers.values()) {
      clearTimeout(timer);
    }
    toastDismissTimers.clear();
  };

  const dismissAppToast = (id) => {
    clearToastDismissTimer(id);
    dismissToast(toastState, id);
  };

  const showToast = ({
    title,
    message = '',
    variant = 'info',
    autoDismiss = true,
  }) => {
    const id = addToast(toastState, { title, message, variant });
    if (!autoDismiss) {
      return;
    }

    const timer = setTimeout(() => {
      dismissToast(toastState, id);
      toastDismissTimers.delete(id);
    }, TOAST_AUTO_DISMISS_MS);
    toastDismissTimers.set(id, timer);
  };

  const handleServerEvent = async (event) => {
    if (event.type === 'app_error') {
      showToast({
        title: t('errors.appError', 'Error'),
        message: event.payload?.message ?? '',
        variant: 'error',
        autoDismiss: false,
      });
      return;
    }

    if (event.type === 'provider_auth_completed') {
      providerAuthEvent = event;
      return;
    }

    if (RUN_SERVER_EVENT_TYPES.has(event.type)) {
      runServerEvents = [...runServerEvents, event].slice(
        -MAX_RUN_SERVER_EVENTS,
      );
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

  const navigateToVoiceSettings = () => {
    settingsPanelTarget = 'voice';
    settingsPanelTargetRequestId += 1;
    selectView('settings');
  };

  const handleDebugEnabledChange = (enabled) => {
    const isEnabled = enabled === true;
    debugEnabled = isEnabled;
    if (!isEnabled && activeViewId === 'debug') {
      selectView('settings');
    }
  };

  onMount(() => {
    let cancelled = false;

    connect(connectionState, { onEvent: handleServerEvent });

    // Detect desktop capabilities and start wakeword status polling
    if (isDesktopAccessor()) {
      waitForDesktopBridge()
        .then((ready) => {
          if (cancelled) {
            return null;
          }
          if (!ready) {
            desktopCapabilities = { wakeword: false };
            return null;
          }
          return getDesktopCapabilities();
        })
        .then((caps) => {
          if (cancelled || !caps) {
            return;
          }
          desktopCapabilities = caps;
          if (caps?.wakeword) {
            cleanupWakewordPoll = onWakewordStatusChange((status) => {
              wakewordStatus = status;
            });
          }
        })
        .catch(() => {
          if (!cancelled) {
            desktopCapabilities = { wakeword: false };
          }
        });
    } else {
      desktopCapabilities = { wakeword: false };
    }

    debugStatus()
      .then((result) => {
        if (!cancelled) {
          debugEnabled = result?.enabled ?? false;
        }
      })
      .catch(() => {
        // debug RPC unavailable — keep debug navigation hidden
      });

    return () => {
      cancelled = true;
      disconnect(connectionState);
      clearToastDismissTimers();
      if (cleanupWakewordPoll) {
        cleanupWakewordPoll();
        cleanupWakewordPoll = null;
      }
    };
  });
</script>

<AppShell
  items={visibleNavigationItems}
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
      {runServerEvents}
      {wakewordStatus}
      {desktopCapabilities}
      onNavigateToVoiceSettings={navigateToVoiceSettings}
    />
  {:else if activeViewId === 'agents'}
    <AgentsView
      sharedSelectedAgentId={selectedAgentId}
      onAgentsChanged={refreshAgents}
      onAgentSelected={selectAgent}
      onToast={showToast}
    />
  {:else if activeViewId === 'cron'}
    <CronView />
  {:else if activeViewId === 'system-prompt'}
    <SystemPromptView onToast={showToast} />
  {:else if activeViewId === 'settings'}
    <SettingsView
      {providerAuthEvent}
      onToast={showToast}
      {agents}
      {desktopCapabilities}
      targetPanelId={settingsPanelTarget}
      targetPanelRequestId={settingsPanelTargetRequestId}
      onDebugEnabledChange={handleDebugEnabledChange}
    />
  {:else if activeViewId === 'logs'}
    <LogsView />
  {:else if activeViewId === 'debug'}
    <DebugView />
  {/if}
  <ToastStack toasts={toastState.toasts} onDismiss={dismissAppToast} />
</AppShell>
