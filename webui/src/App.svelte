<script module>
  export const NAVIGATION_ITEMS = Object.freeze([
    {
      id: 'chat',
      labelKey: 'navigation.chat',
      labelFallback: 'Chat',
      section: 'work',
    },
    {
      id: 'terminals',
      labelKey: 'navigation.terminals',
      labelFallback: 'Terminals',
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
      id: 'calendar',
      labelKey: 'navigation.calendar',
      labelFallback: 'Calendar',
      section: 'work',
    },
    {
      id: 'skills',
      labelKey: 'navigation.skills',
      labelFallback: 'Skills',
      section: 'configure',
    },
    {
      id: 'cron',
      labelKey: 'navigation.cron',
      labelFallback: 'Cron',
      section: 'configure',
    },
    {
      id: 'system-prompt',
      labelKey: 'navigation.systemPrompt',
      labelFallback: 'System Prompt',
      section: 'configure',
    },
    {
      id: 'settings',
      labelKey: 'navigation.settings',
      labelFallback: 'Settings',
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
  import AppShell from './components/AppShell.svelte';
  import Banner from './components/ui/Banner.svelte';
  import { t } from '$lib/i18n.js';
  import Button from './components/ui/Button.svelte';
  import ExtensionRequests from './components/ExtensionRequests.svelte';
  import LiveVoice from './components/LiveVoice.svelte';
  import OnboardingView from './components/OnboardingView.svelte';
  import ChatWorkspace from './components/ChatWorkspace.svelte';
  import { appearancePrefs } from '$lib/appearancePrefs.svelte.js';
  import ExtensionPage from './components/ExtensionPage.svelte';
  import { dateTimePrefs } from '$lib/dateTimePrefs.svelte.js';
  import AgentsView from './components/AgentsView.svelte';
  import TerminalsView from './components/TerminalsView.svelte';
  import ProjectsView from './components/ProjectsView.svelte';
  import CalendarView from './components/CalendarView.svelte';
  import CronView from './components/CronView.svelte';
  import SkillsView from './components/skills/SkillsView.svelte';
  import SystemPromptView from './components/SystemPromptView.svelte';
  import SettingsView from './components/SettingsView.svelte';
  import LogsView from './components/LogsView.svelte';
  import StatisticsView from './components/StatisticsView.svelte';
  import DebugView from './components/DebugView.svelte';
  import ToastStack from './components/ToastStack.svelte';
  import Modal from './components/ui/Modal.svelte';
  import DesktopConnectionSettings from './components/settings/DesktopConnectionSettings.svelte';
  import { onMount, tick } from 'svelte';
  import {
    CONNECTION_STATUS_DISCONNECTED,
    handleVisibilityChange,
  } from '$lib/connectionState.js';
  import {
    createAppController,
    createAppControllerState,
  } from '$lib/appController.js';
  import {
    debugStatus,
    acknowledgeSessionStoreIncident,
    getSessionStoreStatus,
    showProject,
  } from '$lib/api.js';
  import {
    createAutosaveCoordinator,
    provideAutosaveContext,
  } from '$lib/autosave.js';
  import { viewIdFromLocationHash } from '$lib/navigationHistory.js';
  import { createAppSelection } from './app/selection.svelte.js';
  import { createAppSetup } from './app/setup.svelte.js';
  import { createAppDesktop } from './app/desktop.svelte.js';
  import { createAppExtensions } from './app/extensions.svelte.js';
  import './styles/app.css';

  const navigationItems = NAVIGATION_ITEMS;
  const selection = createAppSelection({
    get promptScopeTarget() {
      return promptScopeTarget;
    },
    set promptScopeTarget(value) {
      promptScopeTarget = value;
    },
    get promptScopeTargetRequestId() {
      return promptScopeTargetRequestId;
    },
    set promptScopeTargetRequestId(value) {
      promptScopeTargetRequestId = value;
    },
  });
  const setup = createAppSetup({
    get modelsRefreshToken() {
      return modelsRefreshToken;
    },
    get selectView() {
      return selectView;
    },
  });
  const desktop = createAppDesktop({
    get connectionState() {
      return connectionState;
    },
  });
  const extensions = createAppExtensions({
    get navigationItems() {
      return navigationItems;
    },
    get selectView() {
      return selectView;
    },
  });

  const visibleNavigationItems = $derived(
    debugEnabled
      ? extensions.allNavigationItems
      : extensions.allNavigationItems.filter((item) => item.id !== 'debug'),
  );

  const knownViewIds = () =>
    extensions.allNavigationItems.map((item) => item.id);

  const initialViewId = () => {
    try {
      return (
        viewIdFromLocationHash(window.location.hash, knownViewIds()) ||
        navigationItems[0].id
      );
    } catch {
      return navigationItems[0].id;
    }
  };

  const appControllerState = $state(createAppControllerState(initialViewId()));
  const autosaveCoordinator = createAutosaveCoordinator();
  let appController;
  let activeViewId = $derived(appControllerState.activeViewId);
  $effect.pre(() => extensions.syncRoute(activeViewId));

  let autosaveTransitionSaving = $state(false);
  let autosaveFailureOpen = $state(false);
  let pendingAutosaveTransition = null;
  let debugEnabled = $state(false);

  let liveVoiceView;
  let terminalsView = $state();
  let voiceChatSelection = null;

  // Settings is unmounted when another main view opens. Keep its reading
  // anchor in the long-lived App shell so a normal tab return resumes exactly
  // where the user left the document.
  let settingsScrollPosition = $state(null);
  let settingsView = $state(null);

  let modelsRefreshToken = $derived(appControllerState.modelsRefreshToken);
  let memoriesRefreshToken = $derived(appControllerState.memoriesRefreshToken);
  let projectsRefreshToken = $derived(appControllerState.projectsRefreshToken);
  let sessionsRefreshToken = $derived(appControllerState.sessionsRefreshToken);
  let sessionStoreIncident = $derived(appControllerState.sessionStoreIncident);
  let commandsRefreshToken = $derived(appControllerState.commandsRefreshToken);
  let queueInvalidation = $derived(appControllerState.queueInvalidation);
  let clientsRefreshToken = $derived(appControllerState.clientsRefreshToken);
  let channelsRefreshToken = $derived(appControllerState.channelsRefreshToken);
  let cronRefreshToken = $derived(appControllerState.cronRefreshToken);
  let calendarRefreshToken = $derived(appControllerState.calendarRefreshToken);
  let skillsRefreshToken = $derived(appControllerState.skillsRefreshToken);
  let debugTracesRefreshToken = $derived(
    appControllerState.debugTracesRefreshToken,
  );
  let terminalsRefreshToken = $derived(
    appControllerState.terminalsRefreshToken,
  );
  let connectionState = $derived(appControllerState.connectionState);
  let serverNoticeState = $derived(appControllerState.serverNoticeState);
  let serverRecoveryGeneration = $derived(
    appControllerState.serverRecoveryGeneration,
  );
  let serverUnavailable = $derived(
    connectionState.status === CONNECTION_STATUS_DISCONNECTED,
  );

  let pendingSessionNavigation = $derived(
    appControllerState.pendingSessionNavigation,
  );
  let providerAuthEvent = $derived(appControllerState.providerAuthEvent);
  let runServerEvents = $derived(appControllerState.runServerEvents);
  let backgroundBashStatusEvents = $derived(
    appControllerState.backgroundBashStatusEvents,
  );
  let connectionSnapshot = $derived(appControllerState.connectionSnapshot);

  let serverSwitcherOpen = $state(false);

  let settingsPanelTarget = $derived(appControllerState.settingsPanelTarget);
  let settingsPanelTargetRequestId = $derived(
    appControllerState.settingsPanelTargetRequestId,
  );
  let promptScopeTarget = $derived(appControllerState.promptScopeTarget);
  let promptScopeTargetRequestId = $derived(
    appControllerState.promptScopeTargetRequestId,
  );

  $effect(() => {
    if (!serverUnavailable) {
      serverSwitcherOpen = false;
    }
  });

  const navigateToAgentModel = () => {
    selectView('agents');
  };

  const navigateToProviders = () => {
    navigateToSettingsPanel('providers');
  };

  const navigateToProjects = () => {
    selectView('projects');
  };

  // Calendar → Scheduled Runs deep link: remember the job the user clicked so
  // CronView can preselect it once its list loads.
  let pendingCronJobTarget = $state('');

  const openCronJobFromCalendar = (jobId) => {
    pendingCronJobTarget = typeof jobId === 'string' ? jobId : '';
    selectView('cron');
  };

  const runAutosaveTransition = async (action) => {
    pendingAutosaveTransition = action;
    autosaveTransitionSaving = true;
    const saved = await autosaveCoordinator.flushPending();
    autosaveTransitionSaving = false;

    if (!saved) {
      autosaveFailureOpen = true;
      return false;
    }

    const latestAction = pendingAutosaveTransition;
    pendingAutosaveTransition = null;
    autosaveFailureOpen = false;
    return latestAction?.();
  };

  const requestAutosaveTransition = (action) => {
    if (typeof action !== 'function' || autosaveFailureOpen) return false;
    if (autosaveTransitionSaving) {
      pendingAutosaveTransition = action;
      return false;
    }
    if (!autosaveCoordinator.hasPending()) {
      return action();
    }
    return runAutosaveTransition(action);
  };

  provideAutosaveContext({
    register: autosaveCoordinator.register,
    requestTransition: requestAutosaveTransition,
  });

  const retryAutosaveTransition = () => {
    if (!pendingAutosaveTransition || autosaveTransitionSaving) {
      return;
    }
    void runAutosaveTransition(pendingAutosaveTransition);
  };

  const discardAutosaveTransition = () => {
    if (!pendingAutosaveTransition || autosaveTransitionSaving) {
      return;
    }
    const action = pendingAutosaveTransition;
    pendingAutosaveTransition = null;
    autosaveFailureOpen = false;
    action();
  };

  const selectView = (viewId) =>
    requestAutosaveTransition(() => {
      // Capture while Settings still owns its DOM. Browser scroll events usually
      // keep this current already, but the navigation boundary must not depend
      // on destroy-hook ordering.
      if (activeViewId === 'settings' && viewId !== 'settings') {
        const position = settingsView?.getScrollPosition?.();
        if (position) {
          settingsScrollPosition = position;
        }
      }
      return appController.selectView(viewId);
    });
  const handleChatSessionNavigation = (override) => {
    voiceChatSelection = override;
    return appController.handleChatSessionNavigation(override);
  };

  const liveContext = async () => {
    const projectId = selection.selectedProjectId;
    const context = {
      view: activeViewId,
      selected_agent_id: selection.selectedAgentId,
      selected_project_id: selection.selectedProjectId,
      selected_project_agent_id: selection.selectedProjectAgentId,
      chat_selection: voiceChatSelection,
      agents: selection.agents.map((agent) => ({
        agent_id: agent.id,
        name: agent.name,
      })),
      projects: selection.projects.map((project) => ({
        project_id: project.project_id,
        name: project.display_name,
        cwd: project.cwd,
      })),
    };
    return {
      ...context,
      selected_project_team: projectId
        ? ((await showProject(projectId)).scan?.team || []).map((agent) => ({
            agent_id: `${agent.agent_id}@${projectId}`,
            name: agent.display_name,
          }))
        : [],
    };
  };

  const liveNavigate = (view, target = {}) =>
    requestAutosaveTransition(() => {
      if (!liveVoiceView?.isActive()) return false;
      if (view === 'chat' && target.session_id) {
        return appController.navigateToSession(
          target.agent_id,
          target.session_id,
        );
      }
      if (activeViewId !== view) return appController.selectView(view);
      return true;
    });

  const liveTerminalAction = async (action, args = {}) => {
    if (action === 'context')
      return terminalsView?.getVoiceContext() ?? { visible_order: [] };
    if ((await liveNavigate('terminals')) === false)
      throw new Error('navigation_not_applied');
    await tick();
    if (!terminalsView || !liveVoiceView?.isActive())
      throw new Error('terminal_view_unavailable');
    return terminalsView.applyVoiceAction(action, args);
  };

  const navigateToSubAgent = (targetOrAgentId, maybeSessionId) =>
    requestAutosaveTransition(() =>
      appController.navigateToSubAgent(targetOrAgentId, maybeSessionId),
    );

  const loadSessionStoreStatus = async () => {
    try {
      const result = await getSessionStoreStatus();
      appControllerState.sessionStoreHealth = result ?? null;
      appControllerState.sessionStoreIncident = result?.incident ?? null;
    } catch {
      // Preserve the last durable incident projection during a transient RPC failure.
    }
  };

  const acknowledgeSessionStoreRecovery = async () => {
    const incidentId = sessionStoreIncident?.incident_id;
    if (!incidentId) {
      return;
    }
    try {
      const result = await acknowledgeSessionStoreIncident(incidentId);
      appControllerState.sessionStoreHealth = result ?? null;
      appControllerState.sessionStoreIncident = result?.incident ?? null;
    } catch (error) {
      desktop.showToast({
        title: t(
          'sessionStore.acknowledgeFailedTitle',
          'Recovery notice still needs attention',
        ),
        message:
          error?.message ??
          t(
            'sessionStore.acknowledgeFailed',
            'Refresh the status and try again.',
          ),
        variant: 'error',
      });
    }
  };

  const connectServerEvents = () => appController.connectServerEvents();

  // Shared defaults have one owner in Agents, including links from Projects.
  // Consume the target after opening so ordinary navigation returns to the Agent.
  let pendingAgentDefaultsPanel = $state('');
  const navigateToAgentDefaults = (panelId = 'defaults') =>
    requestAutosaveTransition(() => {
      pendingAgentDefaultsPanel = panelId === 'agent' ? '' : panelId;
      return appController.selectView('agents');
    });

  const navigateToSettingsPanel = (panelId) => {
    if (panelId === 'defaults' || panelId === 'compaction')
      return navigateToAgentDefaults(panelId);
    requestAutosaveTransition(() => {
      // A deliberate deep link (for example Provider setup) owns the
      // next Settings position; ordinary tab switches leave the memory intact.
      settingsScrollPosition = null;
      return appController.navigateToSettingsPanel(panelId);
    });
  };

  const rememberSettingsScrollPosition = (position) => {
    settingsScrollPosition = position;
  };

  const navigateToVoiceSettings = () => {
    navigateToSettingsPanel('voice');
  };

  // Deep-link to the System Prompt view with a given agent's scope preselected.
  // Mirrors the settings-panel mechanism: a target agent id + a fresh request id
  // SystemPromptView reacts to once scopes have loaded, falling back to the
  // default scope when the target scope is absent.
  const navigateToAgentPromptScope = (agentId) => {
    requestAutosaveTransition(() =>
      appController.navigateToPromptScope(agentId),
    );
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
    currentNavigationSelection: selection.currentNavigationSelection,
    isDebugEnabled: () => debugEnabled,
    isOperational: () => setup.operational,
    onAppError: (message) => {
      desktop.showToast({
        title: t('errors.appError', 'Error'),
        message,
        variant: 'error',
      });
    },
    onLoadProjects: selection.loadProjects,
    onAgentIdChanged: selection.remapIdentityAgentId,
    onReloadAgents: selection.reloadAgentsFromServer,
    onReloadExtensionPages: extensions.loadExtensionPages,
    onLoadSessionStoreStatus: loadSessionStoreStatus,
    onSetOnboardingAside: setup.dismissOnboarding,
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

  export function getProjects() {
    return selection.projects;
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

    const onVisibilityChange = () => {
      handleVisibilityChange(appControllerState.connectionState);
    };
    document.addEventListener('visibilitychange', onVisibilityChange);

    // Load the project list for the chat dropdown (best-effort; the chat works
    // identity-only when this fails).
    selection.loadProjects();
    // Voice routing and other non-Chat views also consume the shared Agent
    // roster, so seed it at app mount instead of relying on ChatView having
    // mounted first.
    void selection.reloadAgentsFromServer();

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
    // drives first-run onboarding. Chat appearance preferences are passed to
    // ChatView; the language seed closes the startup-language gap.
    void setup.loadAppSettings();

    return () => {
      cancelled = true;
      selection.destroy();
      document.removeEventListener('visibilitychange', onVisibilityChange);
      appController.destroy();
    };
  });
  function protectPendingEdits(event) {
    if (!autosaveCoordinator.hasPending()) return;
    event.preventDefault();
    event.returnValue = '';
  }
</script>

<svelte:window onbeforeunload={protectPendingEdits} />

<AppShell
  items={visibleNavigationItems}
  {activeViewId}
  onSelectView={selectView}
  connectionStatus={connectionState.status}
  {serverUnavailable}
  {serverNoticeState}
  showServerNotice={!serverSwitcherOpen}
  onRetryConnection={connectServerEvents}
  canSwitchServer={Boolean(desktop.desktopCapabilities?.serverSelection)}
  onSwitchServer={() => (serverSwitcherOpen = true)}
  desktopContextMenuEnabled={Boolean(desktop.desktopCapabilities?.contextMenu)}
  wakewordStatus={desktop.wakewordStatus}
  desktopCapabilities={desktop.desktopCapabilities}
  onNavigateToVoiceSettings={navigateToVoiceSettings}
  onStopWakewordRecording={desktop.handleStopWakewordRecording}
  onToast={desktop.showToast}
>
  {#if setup.showFinishSetup}
    <Banner variant="info" class="app-finish-setup">
      <span class="app-finish-setup__text">
        {t(
          'onboarding.finishSetupHint',
          'Connect an AI service to start chatting.',
        )}
      </span>
      <Button variant="secondary" onClick={setup.reopenOnboarding}>
        {t('onboarding.finishSetup', 'Finish setup')}
      </Button>
    </Banner>
  {/if}
  <ExtensionRequests />
  {#snippet sidebarFooter()}
    <LiveVoice
      bind:this={liveVoiceView}
      getContext={liveContext}
      navigate={liveNavigate}
      terminalView={liveTerminalAction}
      runEvents={runServerEvents}
      wakewordEnabled={desktop.wakewordStatus.enabled}
      {serverUnavailable}
      enabled={setup.settings?.live_voice?.enabled === true}
      onToast={desktop.showToast}
    />
  {/snippet}
  {#if sessionStoreIncident}
    <Banner variant="error" role="alert" class="app-session-store-incident">
      <div class="app-session-store-incident__copy">
        <strong
          >{t(
            'sessionStore.recoveredTitle',
            'Session storage recovered',
          )}</strong
        >
        <span>
          {t(
            'sessionStore.recoveredMessage',
            'The current Session database was restored from a verified snapshot. Recent changes may be missing.',
          )}
        </span>
        <span class="app-session-store-incident__meta">
          {t('sessionStore.snapshot', 'Snapshot')}: {sessionStoreIncident.restored_snapshot_id}
          · {t('sessionStore.possibleLoss', 'Possible loss')}: {sessionStoreIncident
            .possible_loss_interval?.start}
          → {sessionStoreIncident.possible_loss_interval?.end}
        </span>
      </div>
      <Button variant="secondary" onClick={acknowledgeSessionStoreRecovery}>
        {t('sessionStore.acknowledge', 'Acknowledge')}
      </Button>
    </Banner>
  {/if}
  {#key serverRecoveryGeneration}
    {#if setup.onboardingActive}
      <OnboardingView
        {providerAuthEvent}
        {modelsRefreshToken}
        targetAgentId={selection.selectedAgentId || 'main'}
        onComplete={setup.completeOnboarding}
        onDismiss={setup.dismissOnboarding}
        onToast={desktop.showToast}
      />
    {:else}
      <ChatWorkspace
        onToast={desktop.showToast}
        active={activeViewId === 'chat'}
        sharedAgents={selection.agents}
        sharedSelectedAgentId={selection.selectedAgentId}
        chatWidth={appearancePrefs.chatWidth}
        chatWorkingMode={appearancePrefs.chatWorkingMode}
        projects={selection.projects}
        selectedProjectId={selection.selectedProjectId}
        onProjectSelected={selection.selectProject}
        sharedSelectedProjectAgentId={selection.selectedProjectAgentId}
        onProjectAgentSelected={selection.selectProjectAgent}
        onNavigateToProjects={navigateToProjects}
        agentsRefreshToken={selection.agentsRefreshToken}
        onAgentsChanged={selection.syncAgents}
        onAgentSelected={selection.selectAgent}
        {navigateToSubAgent}
        {pendingSessionNavigation}
        onSessionNavigation={handleChatSessionNavigation}
        {runServerEvents}
        {backgroundBashStatusEvents}
        {connectionSnapshot}
        {sessionsRefreshToken}
        {commandsRefreshToken}
        {queueInvalidation}
        hasConnectedProvider={setup.settings === null
          ? null
          : setup.operational}
        onConnectProvider={navigateToProviders}
        onPickModel={navigateToAgentModel}
      />
      {#if activeViewId.startsWith('extension:')}
        {@const page = extensions.extensionPages.find(
          (item) => item.route === activeViewId,
        )}
        <ExtensionPage
          descriptor={page}
          route={extensions.extensionPageRoute}
          theme={{ ...extensions.extensionPageTheme }}
          locale={setup.settings?.appearance?.language ?? 'en'}
          timezone={dateTimePrefs.timeZone}
          invalidation={page
            ? {
                owner: page.extension,
                page: page.page,
                revision: extensions.extensionPageInvalidationRevision,
              }
            : null}
          onRouteChange={(route) => {
            extensions.extensionPageRoute = route;
          }}
          onToast={(message, variant) =>
            desktop.showToast({ title: message, variant })}
        />
      {:else if activeViewId === 'agents'}
        <AgentsView
          sharedSelectedAgentId={selection.selectedAgentId}
          targetDefaultsPanel={pendingAgentDefaultsPanel}
          onDefaultsTargetHandled={() => (pendingAgentDefaultsPanel = '')}
          onAgentsChanged={selection.refreshAgents}
          onAgentSelected={selection.selectAgent}
          onToast={desktop.showToast}
          onNavigateToSettingsPanel={navigateToSettingsPanel}
          onNavigateToAgentPrompt={navigateToAgentPromptScope}
          agentsRefreshToken={selection.agentsRefreshToken}
          {memoriesRefreshToken}
          {modelsRefreshToken}
          {projectsRefreshToken}
        />
      {:else if activeViewId === 'terminals'}
        <TerminalsView
          bind:this={terminalsView}
          {terminalsRefreshToken}
          {serverUnavailable}
          onToast={desktop.showToast}
        />
      {:else if activeViewId === 'projects'}
        <ProjectsView
          selectedProjectId={selection.managedProjectId}
          onProjectSelected={selection.selectManagedProject}
          onToast={desktop.showToast}
          onNavigateToSettingsPanel={navigateToSettingsPanel}
          {modelsRefreshToken}
          {projectsRefreshToken}
        />
      {:else if activeViewId === 'calendar'}
        <CalendarView
          onToast={desktop.showToast}
          {serverUnavailable}
          {calendarRefreshToken}
          onOpenCronJob={openCronJobFromCalendar}
          onOpenSession={(target, session) =>
            requestAutosaveTransition(() =>
              appController.navigateToSession(target, session),
            )}
        />
      {:else if activeViewId === 'cron'}
        <CronView
          onToast={desktop.showToast}
          {serverUnavailable}
          {cronRefreshToken}
          agentsRefreshToken={selection.agentsRefreshToken}
          {projectsRefreshToken}
          targetJobId={pendingCronJobTarget}
        />
      {:else if activeViewId === 'skills'}
        <SkillsView
          onToast={desktop.showToast}
          settings={setup.settings}
          onSettingsCommit={(nextSettings) => (setup.settings = nextSettings)}
          {skillsRefreshToken}
        />
      {:else if activeViewId === 'system-prompt'}
        <SystemPromptView
          onToast={desktop.showToast}
          targetScopeAgentId={promptScopeTarget}
          targetScopeRequestId={promptScopeTargetRequestId}
        />
      {:else if activeViewId === 'settings'}
        <SettingsView
          bind:this={settingsView}
          onSettingsCommit={(nextSettings) => (setup.settings = nextSettings)}
          onNavigateToAgentDefaults={navigateToAgentDefaults}
          {providerAuthEvent}
          onToast={desktop.showToast}
          agents={selection.agents}
          desktopCapabilities={desktop.desktopCapabilities}
          targetPanelId={settingsPanelTarget}
          targetPanelRequestId={settingsPanelTargetRequestId}
          onDebugEnabledChange={handleDebugEnabledChange}
          onOpenSetupGuide={setup.reopenOnboarding}
          {modelsRefreshToken}
          {clientsRefreshToken}
          {channelsRefreshToken}
          initialScrollPosition={settingsScrollPosition}
          onScrollPositionChange={rememberSettingsScrollPosition}
        />
      {:else if activeViewId === 'logs'}
        <LogsView />
      {:else if activeViewId === 'statistics'}
        <StatisticsView />
      {:else if activeViewId === 'debug'}
        <DebugView {debugTracesRefreshToken} />
      {/if}
    {/if}
  {/key}
  <ToastStack
    toasts={desktop.toastState.toasts}
    onDismiss={desktop.dismissAppToast}
  />
</AppShell>

{#if autosaveFailureOpen}
  <Modal
    title={t('autosave.transitionFailureTitle', 'Changes could not be saved')}
    labelledById="autosave-transition-failure-title"
    closeDisabled={true}
    onClose={() => {}}
  >
    {#snippet body()}
      <div class="modal-body">
        <p>
          {t(
            'autosave.transitionFailureBody',
            'Your changes are still open. Try saving again, or discard them and continue.',
          )}
        </p>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="primary"
        disabled={autosaveTransitionSaving}
        onClick={retryAutosaveTransition}
      >
        {autosaveTransitionSaving
          ? t('common.saving', 'Saving…')
          : t('common.retry', 'Retry')}
      </Button>
      <Button
        variant="danger"
        disabled={autosaveTransitionSaving}
        onClick={discardAutosaveTransition}
      >
        {t('autosave.discardAndContinue', 'Discard and continue')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if serverSwitcherOpen}
  <Modal
    title={t('settings.desktop.switchModalTitle', 'Switch server')}
    labelledById="desktop-server-switch-title"
    class="desktop-server-switch-modal"
    onClose={() => (serverSwitcherOpen = false)}
  >
    {#snippet body()}
      <div class="modal-body desktop-server-switch-modal__body">
        <DesktopConnectionSettings
          idPrefix="desktop-outage-server"
          onToast={desktop.showToast}
        />
      </div>
    {/snippet}
  </Modal>
{/if}

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

  :global(.app-session-store-incident) {
    position: sticky;
    top: 0;
    z-index: 4;
    flex-shrink: 0;
    align-items: center;
    gap: 16px;
    padding: 10px 20px;
    border-width: 0 0 1px 3px;
    border-radius: 0;
    box-shadow: 0 8px 24px rgb(20 20 18 / 8%);
  }

  .app-session-store-incident__copy {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .app-session-store-incident__copy span {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }

  .app-session-store-incident__meta {
    overflow-wrap: anywhere;
    font-size: var(--fs-caption) !important;
  }

  .app-finish-setup__text {
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
  }

  :global(.desktop-server-switch-modal) {
    width: min(680px, calc(100vw - 40px));
  }

  .desktop-server-switch-modal__body {
    max-height: min(70vh, 680px);
    overflow-y: auto;
  }

  @media (max-width: 640px) {
    :global(.app-finish-setup) {
      padding: 8px 14px;
    }

    :global(.app-session-store-incident) {
      align-items: stretch;
      padding: 10px 14px;
    }
  }
</style>
