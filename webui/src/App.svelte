<script module>
  import { t } from '$lib/i18n.js';

  export const NAVIGATION_ITEMS = Object.freeze([
    {
      id: 'chat',
      label: () => t('navigation.chat'),
      section: 'work',
    },
    {
      id: 'terminals',
      label: () => t('navigation.terminals'),
      section: 'work',
    },
    {
      id: 'agents',
      label: () => t('navigation.agents'),
      section: 'work',
    },
    {
      id: 'projects',
      label: () => t('navigation.projects'),
      section: 'work',
    },
    {
      id: 'calendar',
      label: () => t('navigation.calendar'),
      section: 'work',
    },
    {
      id: 'jev',
      label: () => t('navigation.jev'),
      section: 'work',
    },
    {
      id: 'skills',
      label: () => t('navigation.skills'),
      section: 'configure',
    },
    {
      id: 'cron',
      label: () => t('navigation.cron'),
      section: 'configure',
    },
    {
      id: 'system-prompt',
      label: () => t('navigation.systemPrompt'),
      section: 'configure',
    },
    {
      id: 'settings',
      label: () => t('navigation.settings'),
      section: 'configure',
    },
    {
      id: 'statistics',
      label: () => t('navigation.statistics'),
      section: 'insights',
    },
    {
      id: 'logs',
      label: () => t('navigation.logs'),
      section: 'insights',
    },
    {
      id: 'debug',
      label: () => t('navigation.debug'),
      section: 'insights',
    },
  ]);
</script>

<script>
  import AppShell from './components/AppShell.svelte';
  import Banner from './components/ui/Banner.svelte';
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
  import JevView from './components/decisions/JevView.svelte';
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
  import { onMount, tick, untrack } from 'svelte';
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
    acknowledgeDataStoreIncident,
    getDataStoreStatus,
    reportClientMetrics,
    showProject,
  } from '$lib/api.js';
  import { startClientMetrics } from '$lib/clientMetrics.js';
  import {
    createAutosaveCoordinator,
    provideAutosaveContext,
  } from '$lib/autosave.js';
  import {
    isDesktopAccessor,
    onDesktopOpenSession,
  } from '$lib/desktopBridge.js';
  import {
    createNavigator,
    provideNavigation,
    sameLocation,
  } from '$lib/navigation.svelte.js';
  import { createAppSelection } from './app/selection.svelte.js';
  import { createAppSetup } from './app/setup.svelte.js';
  import { createAppDesktop } from './app/desktop.svelte.js';
  import { createAppExtensions } from './app/extensions.svelte.js';
  import { createLiveUiActions } from './app/liveUiActions.js';
  import './styles/app.css';

  const navigationItems = NAVIGATION_ITEMS;
  const selection = createAppSelection();
  const setup = createAppSetup({
    get modelsRefreshToken() {
      return modelsRefreshToken;
    },
    get selectView() {
      return showView;
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
      return openView;
    },
    // A startup link to an Extension page resolves once its route is known.
    onPagesLoaded: () => navigator.resolvePendingStart(),
  });

  const visibleNavigationItems = $derived(
    debugEnabled
      ? extensions.allNavigationItems
      : extensions.allNavigationItems.filter((item) => item.id !== 'debug'),
  );

  const isKnownView = (viewId) =>
    extensions.allNavigationItems.some((item) => item.id === viewId);

  const appControllerState = $state(createAppControllerState());
  const autosaveCoordinator = createAutosaveCoordinator();
  let appController;

  let autosaveTransitionSaving = $state(false);
  let autosaveFailureOpen = $state(false);
  let pendingAutosaveTransition = null;
  // Unknown until the first status read; Debug stays reachable meanwhile so a
  // startup link to it is not lost.
  let debugEnabled = $state(null);

  let terminalsView = $state();

  // Settings is unmounted when another main view opens. Keep its reading
  // anchor in the long-lived App shell so a normal tab return resumes exactly
  // where the user left the document.
  let settingsScrollPosition = $state(null);
  let settingsView = $state(null);
  // Cron is unmounted as well; a changed new job waits here until its form
  // opens again.
  let cronNewJobDraft = $state.raw(null);

  let modelsRefreshToken = $derived(appControllerState.modelsRefreshToken);
  let memoriesRefreshToken = $derived(appControllerState.memoriesRefreshToken);
  let projectsRefreshToken = $derived(appControllerState.projectsRefreshToken);
  let sessionsRefreshToken = $derived(appControllerState.sessionsRefreshToken);
  let sessionInvalidations = $derived(appControllerState.sessionInvalidations);
  let dataStoreIncident = $derived(appControllerState.dataStoreIncident);
  let commandsRefreshToken = $derived(appControllerState.commandsRefreshToken);
  let queueInvalidation = $derived(appControllerState.queueInvalidation);
  let sessionDeletion = $derived(appControllerState.sessionDeletion);
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

  let providerAuthEvent = $derived(appControllerState.providerAuthEvent);
  let runServerEvents = $derived(appControllerState.runServerEvents);
  let backgroundBashStatusEvents = $derived(
    appControllerState.backgroundBashStatusEvents,
  );
  let connectionSnapshot = $derived(appControllerState.connectionSnapshot);
  let activeRuns = $derived(appControllerState.activeRuns);

  let serverSwitcherOpen = $state(false);

  $effect(() => {
    if (!serverUnavailable) {
      serverSwitcherOpen = false;
    }
  });

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

  // Every navigation - a click, a deep link, Back or Forward - waits for
  // pending edits here. Leaving Settings also keeps its reading position:
  // captured while Settings still owns its DOM, because scroll events alone
  // must not decide the navigation boundary.
  const navigationGate = (action) =>
    requestAutosaveTransition(() => {
      if (activeViewId === 'settings') {
        const position = settingsView?.getScrollPosition?.();
        if (position) {
          settingsScrollPosition = position;
        }
      }
      return action();
    });

  const desktopAccessor = isDesktopAccessor();
  const navigator = createNavigator({
    defaultView: navigationItems[0].id,
    isKnownView,
    resolveView: (viewId) =>
      viewId === 'debug' && debugEnabled === false ? 'settings' : viewId,
    gate: navigationGate,
    remap: remapRenamedAgents,
    guardExit: desktopAccessor,
    handleInput: desktopAccessor,
  });
  provideNavigation(navigator);
  let activeViewId = $derived(navigator.location.view);

  // The main navigation: a view's last place, or its start when it is shown.
  // Leaving first-run setup for another view sets setup aside.
  const openView = (viewId) => {
    if (viewId !== activeViewId && !setup.operational) {
      setup.dismissOnboarding();
    }
    return navigator.open(viewId);
  };

  // Show a view without leaving the place it shows now.
  const showView = (viewId) =>
    viewId === activeViewId ? false : openView(viewId);

  // Chat's first area reports every place it moves to; App turns places it
  // did not choose itself (Back/Forward, deep links, the main navigation)
  // into a restore request for that area.
  const chatNavigation = navigator.view('chat');
  let chatShownSession = $state(null);
  let pendingSessionNavigation = $state(null);
  let chatRestoreRequestId = 0;
  let lastChatLocation = null;

  // Restores wait for the Agent roster, so an entry's Agent is known when
  // Chat applies it.
  $effect.pre(() => {
    const location = navigator.location;
    if (location.view !== 'chat' || selection.agents.length === 0) return;
    untrack(() => {
      const previous = lastChatLocation;
      lastChatLocation = location;
      if (location.origin === 'view' || sameLocation(previous, location)) {
        return;
      }
      // Chat names its first shown Session itself once it resolves; when it
      // already resolved while hidden, the start place is corrected below.
      if (
        !previous &&
        !chatShownSession &&
        location.place.length === 0 &&
        !location.extra
      ) {
        return;
      }
      const [agentId = '', sessionId = ''] = location.place;
      const subAgent = location.extra?.subAgent === true;
      const shown = chatShownSession;
      if (
        shown?.agentId === agentId &&
        shown.sessionId === sessionId &&
        shown.subAgent === subAgent
      ) {
        return;
      }
      chatRestoreRequestId += 1;
      pendingSessionNavigation = {
        ...(agentId && sessionId
          ? {
              agentId,
              sessionId,
              subAgent,
              followSession: subAgent && location.origin === 'app',
            }
          : { returnToCurrent: true }),
        selection: location.extra?.selection ?? null,
        requestId: chatRestoreRequestId,
      };
    });
  });

  const chatExtra = (subAgent) => ({
    selection: selection.currentNavigationSelection(),
    subAgent: subAgent === true,
  });

  // `session` is the Session Chat's first area shows now (`{agentId,
  // sessionId, subAgent}`). A report while Chat is hidden corrects the place
  // Chat returns to and never pulls the app back to Chat.
  const handleChatSessionNavigation = (session, { replace = false } = {}) => {
    chatShownSession = session ?? null;
    if (!session) return false;
    const place = [session.agentId, session.sessionId];
    const extra = chatExtra(session.subAgent);
    return replace || activeViewId !== 'chat'
      ? chatNavigation.replace(place, { extra })
      : chatNavigation.navigate(place, { extra });
  };

  const navigateToSession = (agentId, sessionId, subAgent = false) => {
    if (!agentId || !sessionId) return false;
    return navigator.navigate('chat', [agentId, sessionId], {
      extra: chatExtra(subAgent),
    });
  };

  // Another view chose a different Agent: Chat follows the shared selection
  // when it is shown next instead of returning to its old place.
  const selectAgentFromView = (agentOrId) => {
    const agentId =
      typeof agentOrId === 'string' ? agentOrId : (agentOrId?.id ?? '');
    if (agentId && agentId !== selection.selectedAgentId) {
      navigator.forget('chat');
    }
    selection.selectAgent(agentId);
  };

  function remapRenamedAgents(location) {
    const resolve = (agentId) =>
      appController?.resolveIdentityAgentId(agentId) ?? agentId;
    const remapScope = (segment) =>
      segment.startsWith('agent:')
        ? `agent:${resolve(segment.slice('agent:'.length))}`
        : segment;
    if (location.view === 'chat') {
      const [agentId, ...rest] = location.place;
      const chatSelection = location.extra?.selection;
      return {
        ...location,
        place:
          agentId && !agentId.includes('@')
            ? [resolve(agentId), ...rest]
            : location.place,
        extra: chatSelection
          ? {
              ...location.extra,
              selection: {
                ...chatSelection,
                agentId: resolve(chatSelection.agentId),
              },
            }
          : location.extra,
      };
    }
    if (location.view === 'agents' && location.place[0]) {
      return {
        ...location,
        place: [resolve(location.place[0]), ...location.place.slice(1)],
      };
    }
    if (location.view === 'skills' || location.view === 'system-prompt') {
      return { ...location, place: location.place.map(remapScope) };
    }
    return location;
  }

  const liveUiActions = createLiveUiActions({
    selection,
    navigator,
    navigateToSession,
    activeView: () => activeViewId,
    requestTransition: requestAutosaveTransition,
    chatSelection: () => chatShownSession,
    loadProject: showProject,
    terminalsView: () => terminalsView,
    afterRender: tick,
  });

  const navigateToSubAgent = (target) =>
    navigateToSession(target?.agentId, target?.sessionId, true);

  // Opens one Session from outside the page, the way Live UI's `open` action
  // does: a startup link or a request of the Desktop app.
  const openSessionLink = ({ agentId, sessionId }) =>
    navigateToSession(agentId, sessionId);

  const loadDataStoreStatus = async () => {
    try {
      const result = await getDataStoreStatus();
      appControllerState.dataStoreIncident = result?.incidents?.[0] ?? null;
    } catch {
      // Preserve the last durable incident projection during a transient RPC failure.
    }
  };

  const acknowledgeDataStoreRecovery = async () => {
    const incidentId = dataStoreIncident?.incident_id;
    if (!incidentId) {
      return;
    }
    try {
      const result = await acknowledgeDataStoreIncident(incidentId);
      appControllerState.dataStoreIncident = result?.incidents?.[0] ?? null;
    } catch (error) {
      desktop.showToast({
        title: t('dataStore.acknowledgeFailedTitle'),
        message: error?.message ?? t('dataStore.acknowledgeFailed'),
        variant: 'error',
      });
    }
  };

  const connectServerEvents = () => appController.connectServerEvents();

  const navigateToAgentModel = () =>
    navigator.navigate('agents', [selection.selectedAgentId]);

  const navigateToProjects = () => openView('projects');

  const openCronJobFromCalendar = (jobId) =>
    navigator.navigate('cron', typeof jobId === 'string' ? [jobId] : []);

  // Shared defaults have one owner in Agents, including links from Projects;
  // 'agent' names the selected Agent's own editor.
  const navigateToAgentDefaults = (panelId = 'defaults') =>
    navigator.navigate(
      'agents',
      panelId === 'agent'
        ? [selection.selectedAgentId]
        : ['~defaults', ...(panelId === 'defaults' ? [] : [panelId])],
    );

  // Settings resolves a page or section id to its canonical place; a section
  // link scrolls to its section.
  const navigateToSettingsPanel = (panelId) => {
    if (panelId === 'defaults' || panelId === 'compaction')
      return navigateToAgentDefaults(panelId);
    return navigator.navigate('settings', [panelId]);
  };

  const navigateToProviders = () => navigateToSettingsPanel('providers');

  const rememberSettingsScrollPosition = (position) => {
    settingsScrollPosition = position;
  };

  const navigateToVoiceSettings = () => {
    navigateToSettingsPanel('voice');
  };

  // Deep-link to the System Prompt editor with a given Agent's scope; the
  // view falls back to the default scope when that scope does not exist.
  const navigateToAgentPromptScope = (agentId) =>
    navigator.navigate('system-prompt', [
      'edit',
      ...(agentId ? [`agent:${agentId}`] : []),
    ]);

  const handleDebugEnabledChange = (enabled) => {
    const isEnabled = enabled === true;
    debugEnabled = isEnabled;
    if (!isEnabled && activeViewId === 'debug') {
      navigator.replace('settings');
    }
  };

  appController = createAppController({
    state: appControllerState,
    onAppError: (message) => {
      desktop.showToast({
        title: t('errors.appError'),
        message,
        variant: 'error',
      });
    },
    onLoadProjects: selection.loadProjects,
    onAgentIdChanged: (oldAgentId, newAgentId) => {
      selection.remapIdentityAgentId(oldAgentId, newAgentId);
      navigator.remapAll();
    },
    onReloadAgents: selection.reloadAgentsFromServer,
    onReloadExtensionPages: extensions.loadExtensionPages,
    onExtensionChange: extensions.publishChange,
    onLoadDataStoreStatus: loadDataStoreStatus,
  });
  navigator.start();

  onMount(() => {
    let cancelled = false;

    const stopClientMetrics = startClientMetrics({
      report: reportClientMetrics,
    });
    const sessionLink = navigator.takeSessionLink();
    if (sessionLink) {
      openSessionLink(sessionLink);
    }
    const stopDesktopSessionRequests = isDesktopAccessor()
      ? onDesktopOpenSession(openSessionLink)
      : () => {};
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
      stopDesktopSessionRequests();
      selection.destroy();
      document.removeEventListener('visibilitychange', onVisibilityChange);
      appController.destroy();
      navigator.destroy();
      stopClientMetrics();
    };
  });
  // Unsaved edits and an unsaved new Cron job ask before the page unloads.
  function protectPendingEdits(event) {
    if (!autosaveCoordinator.hasPending() && !cronNewJobDraft) return;
    event.preventDefault();
    event.returnValue = '';
  }
</script>

<svelte:window onbeforeunload={protectPendingEdits} />

<AppShell
  items={visibleNavigationItems}
  {activeViewId}
  onSelectView={openView}
  connectionStatus={connectionState.status}
  {serverUnavailable}
  {serverNoticeState}
  showServerNotice={!serverSwitcherOpen}
  onRetryConnection={connectServerEvents}
  canSwitchServer={Boolean(desktop.desktopCapabilities?.serverSelection)}
  onSwitchServer={() => (serverSwitcherOpen = true)}
  desktopContextMenuEnabled={Boolean(desktop.desktopCapabilities?.contextMenu)}
  voiceAvailable={desktop.voiceAvailable}
  voiceStatus={desktop.voiceStatus}
  onNavigateToVoiceSettings={navigateToVoiceSettings}
  onStopVoiceRecording={desktop.handleStopVoiceRecording}
  onToast={desktop.showToast}
>
  {#if setup.showFinishSetup}
    <Banner variant="info" class="app-finish-setup">
      <span class="app-finish-setup__text">
        {t('onboarding.finishSetupHint')}
      </span>
      <Button variant="secondary" onClick={setup.reopenOnboarding}>
        {t('onboarding.finishSetup')}
      </Button>
    </Banner>
  {/if}
  <ExtensionRequests
    subscribeInvalidations={extensions.subscribeInvalidations}
  />
  {#snippet sidebarFooter()}
    <LiveVoice
      configured={Boolean(setup.settings?.model_tasks?.live_voice?.target)}
      uiActions={liveUiActions}
      {serverUnavailable}
      voiceStatus={desktop.voiceStatus}
      onToast={desktop.showToast}
    />
  {/snippet}
  {#if dataStoreIncident}
    <Banner variant="error" role="alert" class="app-data-store-incident">
      <div class="app-data-store-incident__copy">
        <strong>{t('dataStore.recoveredTitle')}</strong>
        <span>
          {t('dataStore.recoveredMessage')}
        </span>
        <span class="app-data-store-incident__meta">
          {t('dataStore.database')}: {dataStoreIncident.database}
          · {t('dataStore.snapshot')}: {dataStoreIncident.restored_snapshot_id}
          · {t('dataStore.possibleLoss')}: {dataStoreIncident
            .possible_loss_interval?.start}
          → {dataStoreIncident.possible_loss_interval?.end}
        </span>
      </div>
      <Button variant="secondary" onClick={acknowledgeDataStoreRecovery}>
        {t('dataStore.acknowledge')}
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
        {activeRuns}
        {sessionsRefreshToken}
        {sessionInvalidations}
        {commandsRefreshToken}
        {queueInvalidation}
        {sessionDeletion}
        subscribeExtensionInvalidations={extensions.subscribeInvalidations}
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
          navigation={navigator.view(activeViewId)}
          theme={{ ...extensions.extensionPageTheme }}
          locale={setup.settings?.appearance?.language ?? 'en'}
          timezone={dateTimePrefs.timeZone}
          subscribeInvalidations={extensions.subscribeInvalidations}
          onToast={(message, variant) =>
            desktop.showToast({ title: message, variant })}
        />
      {:else if activeViewId === 'agents'}
        <AgentsView
          navigation={navigator.view('agents')}
          sharedSelectedAgentId={selection.selectedAgentId}
          onAgentsChanged={selection.refreshAgents}
          onAgentSelected={selectAgentFromView}
          onToast={desktop.showToast}
          onNavigateToSettingsPanel={navigateToSettingsPanel}
          onNavigateToAgentPrompt={navigateToAgentPromptScope}
          agentsRefreshToken={selection.agentsRefreshToken}
          {memoriesRefreshToken}
          {modelsRefreshToken}
          {projectsRefreshToken}
          {skillsRefreshToken}
        />
      {:else if activeViewId === 'terminals'}
        <TerminalsView
          bind:this={terminalsView}
          navigation={navigator.view('terminals')}
          {terminalsRefreshToken}
          {serverUnavailable}
          onToast={desktop.showToast}
        />
      {:else if activeViewId === 'projects'}
        <ProjectsView
          navigation={navigator.view('projects')}
          selectedProjectId={selection.managedProjectId}
          onProjectSelected={selection.selectManagedProject}
          onToast={desktop.showToast}
          onNavigateToSettingsPanel={navigateToSettingsPanel}
          {modelsRefreshToken}
          {projectsRefreshToken}
        />
      {:else if activeViewId === 'jev'}
        <JevView
          navigation={navigator.view('jev')}
          onNavigateToSettingsPanel={navigateToSettingsPanel}
        />
      {:else if activeViewId === 'calendar'}
        <CalendarView
          navigation={navigator.view('calendar')}
          onToast={desktop.showToast}
          {serverUnavailable}
          {calendarRefreshToken}
          onOpenCronJob={openCronJobFromCalendar}
          onOpenSession={navigateToSession}
        />
      {:else if activeViewId === 'cron'}
        <CronView
          navigation={navigator.view('cron')}
          bind:newJobDraft={cronNewJobDraft}
          onToast={desktop.showToast}
          {serverUnavailable}
          {cronRefreshToken}
          agentsRefreshToken={selection.agentsRefreshToken}
          {projectsRefreshToken}
        />
      {:else if activeViewId === 'skills'}
        <SkillsView
          navigation={navigator.view('skills')}
          onToast={desktop.showToast}
          settings={setup.settings}
          onSettingsCommit={(nextSettings) => (setup.settings = nextSettings)}
          {skillsRefreshToken}
          agentsRefreshToken={selection.agentsRefreshToken}
          {projectsRefreshToken}
        />
      {:else if activeViewId === 'system-prompt'}
        <SystemPromptView
          navigation={navigator.view('system-prompt')}
          onToast={desktop.showToast}
        />
      {:else if activeViewId === 'settings'}
        <SettingsView
          bind:this={settingsView}
          navigation={navigator.view('settings')}
          onSettingsCommit={(nextSettings) => (setup.settings = nextSettings)}
          onNavigateToAgentDefaults={navigateToAgentDefaults}
          {providerAuthEvent}
          onToast={desktop.showToast}
          agents={selection.agents}
          desktopCapabilities={desktop.desktopCapabilities}
          desktopVoice={desktop.desktopVoice}
          onDebugEnabledChange={handleDebugEnabledChange}
          onOpenSetupGuide={setup.reopenOnboarding}
          {modelsRefreshToken}
          {clientsRefreshToken}
          {channelsRefreshToken}
          initialScrollPosition={settingsScrollPosition}
          onScrollPositionChange={rememberSettingsScrollPosition}
        />
      {:else if activeViewId === 'logs'}
        <LogsView navigation={navigator.view('logs')} />
      {:else if activeViewId === 'statistics'}
        <StatisticsView navigation={navigator.view('statistics')} />
      {:else if activeViewId === 'debug'}
        <DebugView
          navigation={navigator.view('debug')}
          {debugTracesRefreshToken}
        />
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
    title={t('autosave.transitionFailureTitle')}
    labelledById="autosave-transition-failure-title"
    closeDisabled={true}
    onClose={() => {}}
  >
    {#snippet body()}
      <div class="modal-body">
        <p>
          {t('autosave.transitionFailureBody')}
        </p>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="primary"
        disabled={autosaveTransitionSaving}
        onClick={retryAutosaveTransition}
      >
        {autosaveTransitionSaving ? t('common.saving') : t('common.retry')}
      </Button>
      <Button
        variant="danger"
        disabled={autosaveTransitionSaving}
        onClick={discardAutosaveTransition}
      >
        {t('autosave.discardAndContinue')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if serverSwitcherOpen}
  <Modal
    title={t('settings.desktop.switchModalTitle')}
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
    border-color: var(--border) var(--border) var(--border) var(--blue);
    border-radius: 0;
    background: var(--surface);
  }

  :global(.app-data-store-incident) {
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

  .app-data-store-incident__copy {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .app-data-store-incident__copy span {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }

  .app-data-store-incident__meta {
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

    :global(.app-data-store-incident) {
      align-items: stretch;
      padding: 10px 14px;
    }
  }
</style>
