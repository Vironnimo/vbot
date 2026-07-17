<script module>
  export const NAVIGATION_ITEMS = Object.freeze([
    {
      id: 'chat',
      labelKey: 'navigation.chat',
      labelFallback: 'Chat',
      section: 'work',
    },
    {
      id: 'agents',
      labelKey: 'navigation.agents',
      labelFallback: 'Agents',
      section: 'work',
    },
    {
      id: 'projects',
      labelKey: 'navigation.projects',
      labelFallback: 'Projects',
      section: 'work',
    },
    {
      id: 'settings',
      labelKey: 'navigation.settings',
      labelFallback: 'Settings',
      section: 'configure',
    },
    {
      id: 'system-prompt',
      labelKey: 'navigation.systemPrompt',
      labelFallback: 'System Prompt',
      section: 'configure',
    },
    {
      id: 'cron',
      labelKey: 'navigation.cron',
      labelFallback: 'Cron',
      section: 'configure',
    },
    {
      id: 'statistics',
      labelKey: 'navigation.statistics',
      labelFallback: 'Statistics',
      section: 'insights',
    },
    {
      id: 'logs',
      labelKey: 'navigation.logs',
      labelFallback: 'Logs',
      section: 'insights',
    },
    {
      id: 'debug',
      labelKey: 'navigation.debug',
      labelFallback: 'Debug',
      section: 'insights',
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
  import OnboardingView from './components/OnboardingView.svelte';
  import ToastStack from './components/ToastStack.svelte';
  import Banner from './components/ui/Banner.svelte';
  import Button from './components/ui/Button.svelte';
  import { CONNECTION_STATUS_DISCONNECTED } from '$lib/connectionState.js';
  import {
    createAppController,
    createAppControllerState,
  } from '$lib/appController.js';
  import {
    debugStatus,
    getSettings,
    listAgents,
    listProjects,
  } from '$lib/api.js';
  import { init, t } from '$lib/i18n.js';
  import {
    appearancePrefs,
    setChatWidth,
  } from '$lib/appearancePrefs.svelte.js';
  import { viewIdFromLocationHash } from '$lib/navigationHistory.js';
  import { createToastState, addToast, dismissToast } from '$lib/toastState.js';
  import { isOperational } from '$lib/onboarding.js';
  import {
    isDesktopAccessor,
    getDesktopCapabilities,
    onWakewordStatusChange,
    playWakewordCue,
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
  const SELECTED_PROJECT_AGENT_KEY = 'vbot.selectedProjectAgentId';
  const MANAGED_PROJECT_KEY = 'vbot.managedProjectId';
  // Accessor-local UI state only: whether the user set the first-run wizard
  // aside this browser. The real trigger stays the live operational state — a
  // credential removal clears this flag and brings the wizard back on its own.
  const ONBOARDING_DISMISSED_KEY = 'vbot.onboardingDismissed';
  const TOAST_AUTO_DISMISS_MS = 3200;

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

  const readStoredManagedProjectId = () => {
    try {
      if (typeof localStorage === 'undefined') {
        return '';
      }
      return localStorage.getItem(MANAGED_PROJECT_KEY) || '';
    } catch {
      return '';
    }
  };

  // The remembered active agent inside the selected project, restored on reload
  // so the chat returns to the same agent instead of the project default (the
  // default jump is only for a genuine project switch). Three states, so it is
  // read as a tri-state (never collapsed to ''):
  //   - null  → nothing remembered yet → the initial load picks the default
  //   - ''    → an identity agent was active alongside the project → restore it
  //   - 'id'  → restore that team member
  const readStoredSelectedProjectAgentId = () => {
    try {
      if (typeof localStorage === 'undefined') {
        return null;
      }
      return localStorage.getItem(SELECTED_PROJECT_AGENT_KEY);
    } catch {
      return null;
    }
  };

  const readOnboardingDismissed = () => {
    try {
      if (typeof localStorage === 'undefined') {
        return false;
      }
      return localStorage.getItem(ONBOARDING_DISMISSED_KEY) === '1';
    } catch {
      return false;
    }
  };

  const writeOnboardingDismissed = (dismissed) => {
    try {
      if (typeof localStorage === 'undefined') {
        return;
      }
      if (dismissed) {
        localStorage.setItem(ONBOARDING_DISMISSED_KEY, '1');
      } else {
        localStorage.removeItem(ONBOARDING_DISMISSED_KEY);
      }
    } catch {
      // localStorage unavailable (private browsing, storage quota)
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

  const appControllerState = $state(createAppControllerState(initialViewId()));
  let appController;
  let activeViewId = $derived(appControllerState.activeViewId);
  let debugEnabled = $state(false);
  let agents = $state([]);
  let selectedAgentId = $state(readStoredSelectedAgentId());
  const initialSelectedProjectId = readStoredSelectedProjectId();
  // Project context for the two-bar chat. `projects` feeds the chat dropdown;
  // `selectedProjectId` is the chosen project (empty = Personal/identity path).
  let projects = $state([]);
  let selectedProjectId = $state(initialSelectedProjectId);
  // Projects-tab selection is remembered independently so browsing project
  // settings does not silently change the Chat context. A selected Chat
  // project seeds and updates this mirror; otherwise the Projects view keeps
  // the user's last management selection.
  let managedProjectId = $state(
    initialSelectedProjectId || readStoredManagedProjectId(),
  );
  // The remembered active agent inside the selected project (tri-state: null =
  // nothing remembered, '' = identity agent active alongside the project, or a
  // bare team-member id). Persisted like the selected agent/project; ChatView
  // reports changes back through `onProjectAgentSelected`.
  let selectedProjectAgentId = $state(readStoredSelectedProjectAgentId());
  let agentsRefreshToken = $state(0);
  let modelsRefreshToken = $derived(appControllerState.modelsRefreshToken);
  let projectsRefreshToken = $derived(appControllerState.projectsRefreshToken);
  let sessionsRefreshToken = $derived(appControllerState.sessionsRefreshToken);
  let queueInvalidation = $derived(appControllerState.queueInvalidation);
  let clientsRefreshToken = $derived(appControllerState.clientsRefreshToken);
  let channelsRefreshToken = $derived(appControllerState.channelsRefreshToken);
  let cronRefreshToken = $derived(appControllerState.cronRefreshToken);
  let debugTracesRefreshToken = $derived(
    appControllerState.debugTracesRefreshToken,
  );
  let connectionState = $derived(appControllerState.connectionState);
  let serverNoticeState = $derived(appControllerState.serverNoticeState);
  let serverRecoveryGeneration = $derived(
    appControllerState.serverRecoveryGeneration,
  );
  let serverUnavailable = $derived(
    connectionState.status === CONNECTION_STATUS_DISCONNECTED,
  );
  let toastState = $state(createToastState());
  // Application settings, fetched on mount and re-fetched on a provider/model
  // change. Drives the first-run onboarding decision. Null until first loaded.
  let settings = $state(null);
  let onboardingDismissed = $state(readOnboardingDismissed());
  // Sticky once shown: the wizard stays until completed/dismissed, so the
  // connect flip (operational → true) never yanks it before the model step.
  let onboardingActive = $state(false);
  let lastSettingsModelsToken = null;
  let pendingSessionNavigation = $derived(
    appControllerState.pendingSessionNavigation,
  );
  let providerAuthEvent = $derived(appControllerState.providerAuthEvent);
  let runServerEvents = $derived(appControllerState.runServerEvents);
  let connectionSnapshot = $derived(appControllerState.connectionSnapshot);
  let desktopCapabilities = $state(null);
  let wakewordStatus = $state({ enabled: false, state: 'off' });
  let settingsPanelTarget = $derived(appControllerState.settingsPanelTarget);
  let settingsPanelTargetRequestId = $derived(
    appControllerState.settingsPanelTargetRequestId,
  );
  let promptScopeTarget = $derived(appControllerState.promptScopeTarget);
  let promptScopeTargetRequestId = $derived(
    appControllerState.promptScopeTargetRequestId,
  );
  let cleanupWakewordPoll = null;
  let lastWakewordEventSequence = null;
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
      if (managedProjectId) {
        localStorage.setItem(MANAGED_PROJECT_KEY, managedProjectId);
      } else {
        localStorage.removeItem(MANAGED_PROJECT_KEY);
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

  $effect(() => {
    try {
      // Tri-state: null clears the key; '' (identity active) and a team-member
      // id are both stored verbatim so the restore can tell them apart.
      if (selectedProjectAgentId === null) {
        localStorage.removeItem(SELECTED_PROJECT_AGENT_KEY);
      } else {
        localStorage.setItem(
          SELECTED_PROJECT_AGENT_KEY,
          selectedProjectAgentId,
        );
      }
    } catch {
      // localStorage unavailable (private browsing, storage quota)
    }
  });

  let operational = $derived(isOperational(settings));
  // Slim re-entry banner: shown only once the wizard was set aside while the
  // system is still not operational. It disappears the instant a provider is
  // connected (operational flips true).
  let showFinishSetup = $derived(
    settings !== null &&
      !operational &&
      onboardingDismissed &&
      !onboardingActive,
  );

  // Fetch application settings and seed the app-wide appearance from them. Also
  // the source of the operational state that drives onboarding.
  const loadAppSettings = async () => {
    try {
      const result = await getSettings();
      settings = result;
      setChatWidth(result?.appearance?.chat_width);
      const language = result?.appearance?.language;
      if (typeof language === 'string' && language.length > 0) {
        init(language);
      }
      maybeStartOnboarding();
    } catch {
      // settings RPC unavailable — keep the comfortable defaults and leave the
      // onboarding decision untriggered (settings stays null).
    }
  };

  // The guided setup shows once, on the first successful settings load, when
  // vBot is not operational and the user has neither dismissed it nor already
  // navigated elsewhere. It is a one-shot decision (not a reactive latch), so a
  // late settings response never pops the wizard over a view the user opened in
  // the meantime; re-entry afterwards is explicit (the "Finish setup" banner).
  let onboardingEvaluated = false;
  function maybeStartOnboarding() {
    if (onboardingEvaluated || settings === null) {
      return;
    }
    onboardingEvaluated = true;
    if (!operational && !onboardingDismissed) {
      onboardingActive = true;
    }
  }

  // A live operational state clears a stale dismiss, so removing credentials
  // later re-triggers the wizard on its own.
  $effect(() => {
    if (operational && onboardingDismissed) {
      onboardingDismissed = false;
      writeOnboardingDismissed(false);
    }
  });

  // Re-fetch settings on a provider/model change (the same signal that bumps
  // `modelsRefreshToken`) so the operational state stays live.
  $effect(() => {
    if (lastSettingsModelsToken === null) {
      lastSettingsModelsToken = modelsRefreshToken;
      return;
    }
    if (modelsRefreshToken !== lastSettingsModelsToken) {
      lastSettingsModelsToken = modelsRefreshToken;
      void loadAppSettings();
    }
  });

  const completeOnboarding = () => {
    onboardingActive = false;
    onboardingDismissed = false;
    writeOnboardingDismissed(false);
    selectView('chat');
    void loadAppSettings();
  };

  const dismissOnboarding = () => {
    onboardingActive = false;
    onboardingDismissed = true;
    writeOnboardingDismissed(true);
  };

  const reopenOnboarding = () => {
    onboardingActive = true;
  };

  const navigateToAgentModel = () => {
    selectView('agents');
  };

  // ChatView reflects the project dropdown choice back here so the persisted
  // mirror stays current.
  const selectProject = (projectId) => {
    selectedProjectId = typeof projectId === 'string' ? projectId : '';
    if (selectedProjectId) {
      managedProjectId = selectedProjectId;
    }
  };

  const selectManagedProject = (projectId) => {
    managedProjectId = typeof projectId === 'string' ? projectId : '';
  };

  // ChatView reports the active project agent (a team-member id, or '' for an
  // identity agent active alongside the project) so the persisted mirror can
  // restore it on reload. ChatView always reports a string; null stays internal
  // to App (a removed project, see loadProjects).
  const selectProjectAgent = (agentId) => {
    selectedProjectAgentId = typeof agentId === 'string' ? agentId : null;
  };

  const navigateToProjects = () => {
    selectView('projects');
  };

  const loadProjects = async () => {
    try {
      const result = await listProjects();
      projects = Array.isArray(result?.projects) ? result.projects : [];
      // Drop a stale persisted selection if its project no longer exists. The
      // remembered project agent goes with it — it only means anything within a
      // live project.
      if (
        selectedProjectId &&
        !projects.some((project) => project.project_id === selectedProjectId)
      ) {
        selectedProjectId = '';
        selectedProjectAgentId = null;
      }
      if (
        managedProjectId &&
        !projects.some((project) => project.project_id === managedProjectId)
      ) {
        managedProjectId = '';
      }
    } catch {
      // Projects RPC unavailable — keep the chat in the identity-only path.
      projects = [];
    }
  };

  // The selection half of a history entry: which identity agent and which
  // project context were active when the entry was created. Restored together
  // with the session override so Back/Forward re-establish the whole chat
  // context (chips, project bar, and displayed session agree again).
  const currentNavigationSelection = () => ({
    agentId: selectedAgentId,
    projectId: selectedProjectId,
    projectAgentId: selectedProjectAgentId,
  });

  const selectView = (viewId) => appController.selectView(viewId);
  const handleChatSessionNavigation = (override) =>
    appController.handleChatSessionNavigation(override);

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

  const navigateToSubAgent = (targetOrAgentId, maybeSessionId) =>
    appController.navigateToSubAgent(targetOrAgentId, maybeSessionId);

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
      const result = await listAgents();
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
    autoDismiss,
  }) => {
    // A disconnected server is already represented by the global availability
    // notice. Suppress dependent action/load errors so one transport failure
    // cannot flood the active view with duplicate symptoms.
    if (
      variant === 'error' &&
      connectionState.status === CONNECTION_STATUS_DISCONNECTED
    ) {
      return;
    }

    const id = addToast(toastState, { title, message, variant });
    // Error toasts stay until the user dismisses them (a transport/server
    // failure the user must acknowledge); success/info/warn auto-dismiss. An
    // explicit `autoDismiss` from the caller always wins over this default.
    const effectiveAutoDismiss =
      autoDismiss === undefined ? variant !== 'error' : autoDismiss;
    if (!effectiveAutoDismiss) {
      return;
    }

    const timer = setTimeout(() => {
      dismissToast(toastState, id);
      toastDismissTimers.delete(id);
    }, TOAST_AUTO_DISMISS_MS);
    toastDismissTimers.set(id, timer);
  };

  const clearOutageErrorToasts = () => {
    const errorToastIds = new Set(
      toastState.toasts
        .filter((toast) => toast.variant === 'error')
        .map((toast) => toast.id),
    );
    for (const id of errorToastIds) {
      clearToastDismissTimer(id);
    }
    toastState.toasts = toastState.toasts.filter(
      (toast) => !errorToastIds.has(toast.id),
    );
  };

  const connectServerEvents = () => appController.connectServerEvents();

  // Deep-link to a specific Settings panel (Agent defaults, Extensions, Voice…).
  // Sets the target + a fresh request id, then switches to the Settings view;
  // SettingsView selects the panel when the request id changes.
  const navigateToSettingsPanel = (panelId) => {
    appController.navigateToSettingsPanel(panelId);
  };

  const navigateToVoiceSettings = () => {
    navigateToSettingsPanel('voice');
  };

  const applyDesktopWakewordStatus = (status) => {
    wakewordStatus = status;
    const events = Array.isArray(status?.events) ? status.events : [];
    const latestSequence = events.reduce(
      (latest, event) =>
        Number.isFinite(event?.sequence)
          ? Math.max(latest, event.sequence)
          : latest,
      0,
    );
    if (lastWakewordEventSequence === null) {
      // Do not replay sounds that happened before this WebUI mounted.
      lastWakewordEventSequence = latestSequence;
      return;
    }
    for (const event of events) {
      if (
        Number.isFinite(event?.sequence) &&
        event.sequence > lastWakewordEventSequence
      ) {
        void playWakewordCue(event.state);
      }
    }
    lastWakewordEventSequence = Math.max(
      lastWakewordEventSequence,
      latestSequence,
    );
  };

  // Deep-link to the System Prompt view with a given agent's scope preselected.
  // Mirrors the settings-panel mechanism: a target agent id + a fresh request id
  // SystemPromptView reacts to once scopes have loaded, falling back to the
  // default scope when the target scope is absent.
  const navigateToAgentPromptScope = (agentId) => {
    appController.navigateToPromptScope(agentId);
  };

  const handleDebugEnabledChange = (enabled) => {
    const isEnabled = enabled === true;
    debugEnabled = isEnabled;
    if (!isEnabled && activeViewId === 'debug') {
      selectView('settings');
    }
  };

  appController = createAppController({
    state: appControllerState,
    knownViewIds,
    defaultViewId: navigationItems[0].id,
    currentNavigationSelection,
    isDebugEnabled: () => debugEnabled,
    isOperational: () => operational,
    onAppError: (message) => {
      showToast({
        title: t('errors.appError', 'Error'),
        message,
        variant: 'error',
      });
    },
    onClearOutageErrors: clearOutageErrorToasts,
    onLoadProjects: loadProjects,
    onReloadAgents: reloadAgentsFromServer,
    onSetOnboardingAside: () => {
      onboardingActive = false;
      onboardingDismissed = true;
      writeOnboardingDismissed(true);
    },
  });

  // Exposed for tests so the routing in `handleServerEvent` can be verified
  // without depending on ChatView's internal state. Production code reads
  // `connectionSnapshot` via the `<ChatView connectionSnapshot={...} />` prop
  // binding above.
  export function getConnectionSnapshot() {
    return appControllerState.connectionSnapshot;
  }

  // Exposed for tests so the `resource_changed` routing in `handleServerEvent`
  // can be verified without reaching into a child view's reload behavior.
  export function getModelsRefreshToken() {
    return appControllerState.modelsRefreshToken;
  }

  export function getSessionsRefreshToken() {
    return appControllerState.sessionsRefreshToken;
  }

  export function getQueueInvalidation() {
    return appControllerState.queueInvalidation;
  }

  export function getClientsRefreshToken() {
    return appControllerState.clientsRefreshToken;
  }

  export function getChannelsRefreshToken() {
    return appControllerState.channelsRefreshToken;
  }

  export function getDebugTracesRefreshToken() {
    return appControllerState.debugTracesRefreshToken;
  }

  onMount(() => {
    let cancelled = false;

    appController.initializeNavigationHistory();
    connectServerEvents();

    // Load the project list for the chat dropdown (best-effort; the chat works
    // identity-only when this fails).
    loadProjects();
    // Voice routing and other non-Chat views also consume the shared Agent
    // roster, so seed it at app mount instead of relying on ChatView having
    // mounted first.
    void reloadAgentsFromServer();

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
              applyDesktopWakewordStatus(status);
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

    // Seed app-wide appearance preferences and the operational state that
    // drives first-run onboarding. `chat_width` drives the chat reading-column
    // width app-wide (passed to ChatView); the language seed closes the
    // startup-language gap.
    void loadAppSettings();

    return () => {
      cancelled = true;
      appController.destroy();
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
  {serverUnavailable}
  {serverNoticeState}
  onRetryConnection={connectServerEvents}
>
  {#if showFinishSetup}
    <Banner variant="info" class="app-finish-setup">
      <span class="app-finish-setup__text">
        {t(
          'onboarding.finishSetupHint',
          'Connect an AI service to start chatting.',
        )}
      </span>
      <Button variant="secondary" onClick={reopenOnboarding}>
        {t('onboarding.finishSetup', 'Finish setup')}
      </Button>
    </Banner>
  {/if}
  {#key serverRecoveryGeneration}
    {#if onboardingActive}
      <OnboardingView
        {providerAuthEvent}
        {modelsRefreshToken}
        targetAgentId={selectedAgentId || 'main'}
        onComplete={completeOnboarding}
        onDismiss={dismissOnboarding}
        onToast={showToast}
      />
    {:else if activeViewId === 'chat'}
      <ChatView
        sharedAgents={agents}
        sharedSelectedAgentId={selectedAgentId}
        chatWidth={appearancePrefs.chatWidth}
        {projects}
        {selectedProjectId}
        onProjectSelected={selectProject}
        sharedSelectedProjectAgentId={selectedProjectAgentId}
        onProjectAgentSelected={selectProjectAgent}
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
        onPickModel={navigateToAgentModel}
      />
    {:else if activeViewId === 'agents'}
      <AgentsView
        sharedSelectedAgentId={selectedAgentId}
        onAgentsChanged={refreshAgents}
        onAgentSelected={selectAgent}
        onToast={showToast}
        onNavigateToSettingsPanel={navigateToSettingsPanel}
        onNavigateToAgentPrompt={navigateToAgentPromptScope}
        {modelsRefreshToken}
        {projectsRefreshToken}
      />
    {:else if activeViewId === 'projects'}
      <ProjectsView
        selectedProjectId={managedProjectId}
        onProjectSelected={selectManagedProject}
        onToast={showToast}
        onNavigateToSettingsPanel={navigateToSettingsPanel}
        {modelsRefreshToken}
        {projectsRefreshToken}
      />
    {:else if activeViewId === 'cron'}
      <CronView
        onToast={showToast}
        {serverUnavailable}
        {cronRefreshToken}
        {agentsRefreshToken}
        {projectsRefreshToken}
      />
    {:else if activeViewId === 'system-prompt'}
      <SystemPromptView
        onToast={showToast}
        targetScopeAgentId={promptScopeTarget}
        targetScopeRequestId={promptScopeTargetRequestId}
      />
    {:else if activeViewId === 'settings'}
      <SettingsView
        {providerAuthEvent}
        onToast={showToast}
        {agents}
        {desktopCapabilities}
        targetPanelId={settingsPanelTarget}
        targetPanelRequestId={settingsPanelTargetRequestId}
        onDebugEnabledChange={handleDebugEnabledChange}
        onOpenSetupGuide={reopenOnboarding}
        {modelsRefreshToken}
        {clientsRefreshToken}
        {channelsRefreshToken}
      />
    {:else if activeViewId === 'logs'}
      <LogsView />
    {:else if activeViewId === 'statistics'}
      <StatisticsView />
    {:else if activeViewId === 'debug'}
      <DebugView {debugTracesRefreshToken} />
    {/if}
  {/key}
  <ToastStack toasts={toastState.toasts} onDismiss={dismissAppToast} />
</AppShell>

<style>
  /* Slim re-entry banner for a dismissed-but-incomplete first-run setup. Sits
     above the active view inside the content column and disappears the instant
     a provider is connected. */
  :global(.app-finish-setup) {
    flex-shrink: 0;
    gap: 14px;
    padding: 8px 20px;
    border-width: 0 0 1px 3px;
    border-color: var(--border) var(--border) var(--border) var(--accent);
    border-radius: 0;
    background: var(--surface);
  }

  .app-finish-setup__text {
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
  }

  @media (max-width: 640px) {
    :global(.app-finish-setup) {
      padding: 8px 14px;
    }
  }
</style>
