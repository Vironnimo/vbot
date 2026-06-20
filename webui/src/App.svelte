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
      id: 'projects',
      labelKey: 'navigation.projects',
      labelFallback: 'Projects',
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
      id: 'statistics',
      labelKey: 'navigation.statistics',
      labelFallback: 'Statistics',
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
  import ProjectsView from './components/ProjectsView.svelte';
  import SettingsView from './components/SettingsView.svelte';
  import StatisticsView from './components/StatisticsView.svelte';
  import SystemPromptView from './components/SystemPromptView.svelte';
  import ToastStack from './components/ToastStack.svelte';
  import {
    createConnectionState,
    connect,
    disconnect,
  } from '$lib/connectionState.js';
  import { rpc, debugStatus, listProjects } from '$lib/api.js';
  import { init, t } from '$lib/i18n.js';
  import {
    appearancePrefs,
    setChatWidth,
  } from '$lib/appearancePrefs.svelte.js';
  import {
    createNavigationHistoryState,
    isNavigationHistoryState,
    locationHashForView,
    sameSessionOverride,
    viewIdFromLocationHash,
  } from '$lib/navigationHistory.js';
  import { createToastState, addToast, dismissToast } from '$lib/toastState.js';
  import {
    RESOURCE_TOKEN_AGENTS,
    RESOURCE_TOKEN_CLIENTS,
    RESOURCE_TOKEN_MODELS,
    RESOURCE_TOKEN_SESSIONS,
    tokenKeysForKind,
  } from '$lib/resourceInvalidation.js';
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
  const SELECTED_PROJECT_KEY = 'vbot.selectedProjectId';
  const TOAST_AUTO_DISMISS_MS = 3200;
  const MAX_RUN_SERVER_EVENTS = 500;
  const CONNECTION_READY_EVENT_TYPE = 'connection_ready';
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

  // The persisted project selection follows the same localStorage pattern as
  // the selected agent (own key). Empty = "No project" / Personal.
  const readStoredSelectedProjectId = () => {
    try {
      if (typeof localStorage === 'undefined') {
        return '';
      }
      return localStorage.getItem(SELECTED_PROJECT_KEY) || '';
    } catch {
      return '';
    }
  };

  const knownViewIds = navigationItems.map((item) => item.id);

  const initialViewId = () => {
    try {
      return (
        viewIdFromLocationHash(window.location.hash, knownViewIds) ||
        navigationItems[0].id
      );
    } catch {
      return navigationItems[0].id;
    }
  };

  let activeViewId = $state(initialViewId());
  let debugEnabled = $state(false);
  let agents = $state([]);
  let selectedAgentId = $state(readStoredSelectedAgentId());
  // Project context for the two-bar chat. `projects` feeds the chat dropdown;
  // `selectedProjectId` is the chosen project (empty = Personal/identity path).
  let projects = $state([]);
  let selectedProjectId = $state(readStoredSelectedProjectId());
  let agentsRefreshToken = $state(0);
  // Bumped by the generic `resource_changed` channel whenever model-catalog or
  // provider availability changes; model surfaces reload on each bump.
  let modelsRefreshToken = $state(0);
  // Bumped by `resource_changed(kind:"sessions")`. ChatView forwards it to the
  // session drawer so a new/switched session in another window shows up in the
  // list — it deliberately does NOT switch the viewed conversation (other
  // windows "stay put").
  let sessionsRefreshToken = $state(0);
  // Scope of the latest `resource_changed(kind:"queue")` — a fresh object per
  // signal so ChatView's effect re-fires. Carries the scope (not a bare token)
  // because the watcher only re-syncs a queue for a session it actually holds.
  let queueInvalidation = $state(null);
  // Bumped by `resource_changed(kind:"clients")` — a window connecting or
  // disconnecting. The General settings panel reloads its presence roster.
  let clientsRefreshToken = $state(0);
  let connectionState = $state(createConnectionState());
  let toastState = $state(createToastState());
  let pendingSessionNavigation = $state(null);
  let providerAuthEvent = $state(null);
  let runServerEvents = $state([]);
  // Holds the most recent `/ws` `connection_ready` hello frame (epoch,
  // last_sequence, active_runs). The frame has no `payload.run_id`/
  // `run_event_sequence`, so `runServerEvents` cannot ingest it — it lives
  // alongside the lifecycle list and is forwarded to ChatView as a separate
  // prop. ChatView decides what (if anything) to do with the snapshot.
  let connectionSnapshot = $state(null);
  let desktopCapabilities = $state(null);
  let wakewordStatus = $state({ enabled: false, state: 'off' });
  let settingsPanelTarget = $state('');
  let settingsPanelTargetRequestId = $state(0);
  let sessionNavigationRequestId = 0;
  // Mirror of ChatView's accessor-local session override (sub-agent session or
  // drawer selection), kept so history entries can encode it and history-driven
  // restores can be distinguished from new user navigation. Only read inside
  // handlers — no reactivity needed.
  let chatSessionOverride = null;
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

  $effect(() => {
    try {
      if (selectedProjectId) {
        localStorage.setItem(SELECTED_PROJECT_KEY, selectedProjectId);
      } else {
        localStorage.removeItem(SELECTED_PROJECT_KEY);
      }
    } catch {
      // localStorage unavailable (private browsing, storage quota)
    }
  });

  // ChatView reflects the project dropdown choice back here so the persisted
  // mirror stays current.
  const selectProject = (projectId) => {
    selectedProjectId = typeof projectId === 'string' ? projectId : '';
  };

  const navigateToProjects = () => {
    selectView('projects');
  };

  const loadProjects = async () => {
    try {
      const result = await listProjects();
      projects = Array.isArray(result?.projects) ? result.projects : [];
      // Drop a stale persisted selection if its project no longer exists.
      if (
        selectedProjectId &&
        !projects.some((project) => project.project_id === selectedProjectId)
      ) {
        selectedProjectId = '';
      }
    } catch {
      // Projects RPC unavailable — keep the chat in the identity-only path.
      projects = [];
    }
  };

  const pushNavigationState = () => {
    try {
      history.pushState(
        createNavigationHistoryState(activeViewId, chatSessionOverride),
        '',
        locationHashForView(activeViewId),
      );
    } catch {
      // History API unavailable (non-browser environment)
    }
  };

  const selectView = (viewId) => {
    if (viewId === activeViewId) {
      return;
    }
    if (activeViewId === 'chat') {
      // ChatView unmounts and loses its local session override with it; a
      // stale pending navigation must not re-apply on the next chat mount.
      chatSessionOverride = null;
      pendingSessionNavigation = null;
    }
    activeViewId = viewId;
    pushNavigationState();
  };

  // ChatView reports user-initiated session-override changes (drawer
  // selection, return-to-current, override cleared by an agent switch) so they
  // become history entries. History-driven restores arrive back through
  // `pendingSessionNavigation` and are reported nowhere, so they cannot
  // re-push; this handler also dedups against the mirror for safety.
  const handleChatSessionNavigation = (override) => {
    const next = override ?? null;
    if (sameSessionOverride(chatSessionOverride, next)) {
      return;
    }
    chatSessionOverride = next;
    pushNavigationState();
  };

  const applyNavigationState = (navState) => {
    let viewId = knownViewIds.includes(navState.view)
      ? navState.view
      : navigationItems[0].id;
    if (viewId === 'debug' && !debugEnabled) {
      viewId = 'settings';
    }
    activeViewId = viewId;

    if (viewId !== 'chat') {
      chatSessionOverride = null;
      pendingSessionNavigation = null;
      return;
    }

    const target = navState.session ?? null;
    if (sameSessionOverride(chatSessionOverride, target)) {
      return;
    }
    chatSessionOverride = target;
    sessionNavigationRequestId += 1;
    pendingSessionNavigation = target
      ? { ...target, requestId: sessionNavigationRequestId }
      : { returnToCurrent: true, requestId: sessionNavigationRequestId };
  };

  const handlePopState = (event) => {
    if (isNavigationHistoryState(event.state)) {
      applyNavigationState(event.state);
      return;
    }
    // Entry without our state (e.g. a manually edited hash): derive the view
    // from the hash and treat the chat surface as override-free.
    const viewId =
      viewIdFromLocationHash(window.location.hash, knownViewIds) ||
      navigationItems[0].id;
    applyNavigationState(createNavigationHistoryState(viewId, null));
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
    handleChatSessionNavigation({ agentId, sessionId, subAgent: true });
    sessionNavigationRequestId += 1;
    pendingSessionNavigation = {
      agentId,
      sessionId,
      subAgent: true,
      requestId: sessionNavigationRequestId,
    };
  };

  const refreshAgents = (nextAgents = []) => {
    syncAgents(nextAgents);
    agentsRefreshToken += 1;
  };

  // Re-fetch the agent roster after a `resource_changed(kind:"agents")` signal
  // (the migrated agent-CRUD reload — the channel carries no agent data, so we
  // re-fetch agent.list). `refreshAgents` bumps `agentsRefreshToken`, so the
  // Agents and Chat surfaces reload exactly as they did for the old agent.*
  // events.
  const reloadAgentsFromServer = async () => {
    try {
      const result = await rpc('agent.list');
      refreshAgents(result.agents);
    } catch (error) {
      console.warn('Agent list refresh failed:', error);
    }
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

    if (event.type === CONNECTION_READY_EVENT_TYPE) {
      // Stash the full hello frame so ChatView can hydrate from the snapshot
      // instead of relying on the WS replay buffer. Do NOT append to
      // `runServerEvents`: the frame has no `run_id`/`run_event_sequence`,
      // so `runServerEventKey` would drop it on the floor.
      connectionSnapshot = event;
      return;
    }

    if (RUN_SERVER_EVENT_TYPES.has(event.type)) {
      runServerEvents = [...runServerEvents, event].slice(
        -MAX_RUN_SERVER_EVENTS,
      );
      return;
    }

    if (event.type === 'resource_changed') {
      // The signal carries only a `kind` (plus an optional scope); route it to
      // the refresh token(s)/reload it invalidates and let the watching surfaces
      // re-fetch through their normal RPCs.
      const kind = event.payload?.kind;
      const tokenKeys = tokenKeysForKind(kind);
      if (tokenKeys.includes(RESOURCE_TOKEN_MODELS)) {
        modelsRefreshToken += 1;
      }
      if (tokenKeys.includes(RESOURCE_TOKEN_SESSIONS)) {
        sessionsRefreshToken += 1;
      }
      if (kind === 'queue') {
        // A fresh object per signal so ChatView's effect re-fires even for a
        // repeat scope; ChatView re-syncs the matching held session's queue.
        const scope = event.payload?.scope ?? {};
        queueInvalidation = {
          agentId: typeof scope.agent_id === 'string' ? scope.agent_id : '',
          sessionId:
            typeof scope.session_id === 'string' ? scope.session_id : '',
        };
      }
      if (tokenKeys.includes(RESOURCE_TOKEN_CLIENTS)) {
        clientsRefreshToken += 1;
      }
      if (tokenKeys.includes(RESOURCE_TOKEN_AGENTS)) {
        await reloadAgentsFromServer();
      }
      return;
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

  // Exposed for tests so the routing in `handleServerEvent` can be verified
  // without depending on ChatView's internal state. Production code reads
  // `connectionSnapshot` via the `<ChatView connectionSnapshot={...} />` prop
  // binding above.
  export function getConnectionSnapshot() {
    return connectionSnapshot;
  }

  // Exposed for tests so the `resource_changed` routing in `handleServerEvent`
  // can be verified without reaching into a child view's reload behavior.
  export function getModelsRefreshToken() {
    return modelsRefreshToken;
  }

  export function getSessionsRefreshToken() {
    return sessionsRefreshToken;
  }

  export function getQueueInvalidation() {
    return queueInvalidation;
  }

  export function getClientsRefreshToken() {
    return clientsRefreshToken;
  }

  onMount(() => {
    let cancelled = false;

    try {
      // Seed the current history entry so Back can always restore it; later
      // navigation pushes new entries on top.
      history.replaceState(
        createNavigationHistoryState(activeViewId, null),
        '',
        locationHashForView(activeViewId),
      );
    } catch {
      // History API unavailable (non-browser environment)
    }
    window.addEventListener('popstate', handlePopState);

    connect(connectionState, { onEvent: handleServerEvent });

    // Load the project list for the chat dropdown (best-effort; the chat works
    // identity-only when this fails).
    loadProjects();

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
          // Also leaves the Debug view when the initial hash pointed at it
          // while Debug Mode is disabled.
          handleDebugEnabledChange(result?.enabled ?? false);
        }
      })
      .catch(() => {
        // debug RPC unavailable — keep debug navigation hidden
      });

    // Seed app-wide appearance preferences once. `chat_width` drives the chat
    // reading-column width app-wide (passed down to ChatView); the language
    // seed is free here and closes the startup-language gap.
    rpc('settings.get')
      .then((settings) => {
        if (cancelled) {
          return;
        }
        setChatWidth(settings?.appearance?.chat_width);
        const language = settings?.appearance?.language;
        if (typeof language === 'string' && language.length > 0) {
          init(language);
        }
      })
      .catch(() => {
        // settings RPC unavailable — keep the comfortable default.
      });

    return () => {
      cancelled = true;
      window.removeEventListener('popstate', handlePopState);
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
      chatWidth={appearancePrefs.chatWidth}
      {projects}
      {selectedProjectId}
      onProjectSelected={selectProject}
      onNavigateToProjects={navigateToProjects}
      {agentsRefreshToken}
      onAgentsChanged={syncAgents}
      onAgentSelected={selectAgent}
      {navigateToSubAgent}
      {pendingSessionNavigation}
      onSessionNavigation={handleChatSessionNavigation}
      {runServerEvents}
      {connectionSnapshot}
      {sessionsRefreshToken}
      {queueInvalidation}
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
      {modelsRefreshToken}
    />
  {:else if activeViewId === 'projects'}
    <ProjectsView onToast={showToast} {modelsRefreshToken} />
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
      {modelsRefreshToken}
      {clientsRefreshToken}
    />
  {:else if activeViewId === 'logs'}
    <LogsView />
  {:else if activeViewId === 'statistics'}
    <StatisticsView />
  {:else if activeViewId === 'debug'}
    <DebugView />
  {/if}
  <ToastStack toasts={toastState.toasts} onDismiss={dismissAppToast} />
</AppShell>
