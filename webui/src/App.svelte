<script module>
  import { t } from '$lib/i18n.js';

  const loadOnboardingView = () => import('./components/OnboardingView.svelte');
  const loadExtensionPage = () => import('./components/ExtensionPage.svelte');
  const loadAgentsView = () => import('./components/AgentsView.svelte');
  const loadTerminalsView = () => import('./components/TerminalsView.svelte');
  const loadProjectsView = () => import('./components/ProjectsView.svelte');
  const loadCalendarView = () => import('./components/CalendarView.svelte');
  const loadCronView = () => import('./components/CronView.svelte');
  const loadSkillsView = () => import('./components/skills/SkillsView.svelte');
  const loadSystemPromptView = () =>
    import('./components/SystemPromptView.svelte');
  const loadSettingsView = () => import('./components/SettingsView.svelte');
  const loadLogsView = () => import('./components/LogsView.svelte');
  const loadStatisticsView = () => import('./components/StatisticsView.svelte');
  const loadDesktopConnectionSettings = () =>
    import('./components/settings/DesktopConnectionSettings.svelte');

  export const NAVIGATION_ITEMS = Object.freeze([
    {
      id: 'chat',
      label: () => t('navigation.chat'),
      section: 'main',
    },
    {
      id: 'terminals',
      label: () => t('navigation.terminals'),
      section: 'main',
    },
    {
      id: 'agents',
      label: () => t('navigation.agents'),
      section: 'agents',
    },
    {
      id: 'skills',
      label: () => t('navigation.skills'),
      section: 'agents',
    },
    {
      id: 'system-prompt',
      label: () => t('navigation.systemPrompt'),
      section: 'agents',
    },
    {
      id: 'projects',
      label: () => t('navigation.projects'),
      section: 'agents',
    },
    {
      id: 'cron',
      label: () => t('navigation.cron'),
      section: 'planning',
    },
    {
      id: 'calendar',
      label: () => t('navigation.calendar'),
      section: 'planning',
    },
    {
      id: 'statistics',
      label: () => t('navigation.statistics'),
      section: 'system',
    },
    {
      id: 'logs',
      label: () => t('navigation.logs'),
      section: 'system',
    },
    {
      id: 'settings',
      label: () => t('navigation.settings'),
      section: 'settings',
    },
  ]);
</script>

<script>
  import AppShell from './components/AppShell.svelte';
  import Banner from './components/ui/Banner.svelte';
  import Button from './components/ui/Button.svelte';
  import LazyContent from './components/ui/LazyContent.svelte';
  import ExtensionRequests from './components/ExtensionRequests.svelte';
  import LiveVoice from './components/LiveVoice.svelte';
  import ChatWorkspace from './components/ChatWorkspace.svelte';
  import { appearancePrefs } from '$lib/appearancePrefs.svelte.js';
  import { dateTimePrefs } from '$lib/dateTimePrefs.svelte.js';
  import ToastStack from './components/ToastStack.svelte';
  import Modal from './components/ui/Modal.svelte';
  import ConfirmDialog from './components/ui/ConfirmDialog.svelte';
  import { onDestroy, onMount, tick } from 'svelte';
  import {
    createSpeechAudio,
    provideSpeechAudio,
  } from '$lib/speechAudio.svelte.js';
  import SpeechPlaybackBar from './components/SpeechPlaybackBar.svelte';
  import {
    CONNECTION_STATUS_DISCONNECTED,
    handleVisibilityChange,
  } from '$lib/connectionState.js';
  import { createScheduleActivity } from './app/scheduleActivity.svelte.js';
  import {
    createAppController,
    createAppControllerState,
  } from '$lib/appController.js';
  import { getServedWebuiBuild, reportClientMetrics } from '$lib/api.js';
  import { startClientMetrics } from '$lib/clientMetrics.js';
  import { loadComponent } from '$lib/lazyContent.js';
  import { isWebuiOutdated } from '$lib/webuiBuild.js';
  import { provideAutosaveContext } from '$lib/autosave.js';
  import {
    flushComposerMemory,
    renameComposerAgent,
  } from '$lib/composerMemory.js';
  import { renameWidgetStatesAgent } from '$lib/widgetStates.svelte.js';
  import { invalidatePortals } from '$lib/portals.svelte.js';
  import {
    isDesktopAccessor,
    onDesktopOpenSession,
    onDesktopRestartRequest,
  } from '$lib/desktopBridge.js';
  import {
    createNavigator,
    provideNavigation,
  } from '$lib/navigation.svelte.js';
  import { createAppSelection } from './app/selection.svelte.js';
  import { createAppSetup } from './app/setup.svelte.js';
  import { createAppDesktop } from './app/desktop.svelte.js';
  import { createAppExtensions } from './app/extensions.svelte.js';
  import { createLiveUiActions } from './app/liveUiActions.js';
  import { createAppAutosave } from './app/autosave.svelte.js';
  import AutosaveTransitionPrompt from './app/AutosaveTransitionPrompt.svelte';
  import { createAppChatNavigation } from './app/chatNavigation.svelte.js';
  import './styles/app.css';

  const navigationItems = NAVIGATION_ITEMS;
  const selection = createAppSelection();
  const speechAudio = provideSpeechAudio(createSpeechAudio());
  onDestroy(() => speechAudio.destroy());
  const setup = createAppSetup({
    get modelsRefreshToken() {
      return modelsRefreshToken;
    },
    get selectView() {
      return openView;
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

  const visibleNavigationItems = $derived(extensions.allNavigationItems);

  // Main menu indicators by view id (`{count, tone, label}`), each reported
  // by the view that owns its meaning.
  const navigationIndicators = $state({});

  const isKnownView = (viewId) =>
    extensions.allNavigationItems.some((item) => item.id === viewId);

  const appControllerState = $state(createAppControllerState());
  const autosave = createAppAutosave({
    onStay: () => navigator.cancelPendingMove(),
  });
  provideAutosaveContext(autosave);
  let appController;

  let terminalsView = $state();

  // Settings is unmounted when another main view opens. Keep its reading
  // anchor in the long-lived App shell so a normal tab return resumes exactly
  // where the user left the document.
  let settingsScrollPosition = $state(null);
  let settingsView = $state(null);
  // Cron is unmounted as well; a changed new job waits here until its form
  // opens again.
  let cronNewJobDraft = $state.raw(null);
  let restartDiscardConfirmOpen = $state(false);

  let modelsRefreshToken = $derived(appControllerState.modelsRefreshToken);
  let recallIndexStatus = $derived(appControllerState.recallIndexStatus);
  let backgroundActivity = $derived(appControllerState.backgroundActivity);
  let secretStore = $derived(appControllerState.secretStore);
  let memoriesRefreshToken = $derived(appControllerState.memoriesRefreshToken);
  let projectsRefreshToken = $derived(appControllerState.projectsRefreshToken);
  let sessionsRefreshToken = $derived(appControllerState.sessionsRefreshToken);
  let archiveRefreshToken = $derived(appControllerState.archiveRefreshToken);
  let sessionInvalidations = $derived(appControllerState.sessionInvalidations);
  let dataStoreIncident = $derived(appControllerState.dataStoreIncident);
  let webuiOutdated = $derived(appControllerState.webuiOutdated);
  let commandsRefreshToken = $derived(appControllerState.commandsRefreshToken);
  let queueInvalidation = $derived(appControllerState.queueInvalidation);
  let sessionDeletion = $derived(appControllerState.sessionDeletion);
  let clientsRefreshToken = $derived(appControllerState.clientsRefreshToken);
  let devicesRefreshToken = $derived(appControllerState.devicesRefreshToken);
  let credentialsRefreshToken = $derived(
    appControllerState.credentialsRefreshToken,
  );
  let agentSecretsRefreshToken = $derived(
    appControllerState.agentSecretsRefreshToken,
  );
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
  let commandStatuses = $derived(appControllerState.commandStatuses);
  let sessionActivity = $derived(appControllerState.sessionActivity);
  let connectionSnapshot = $derived(appControllerState.connectionSnapshot);

  const schedules = createScheduleActivity({
    get sessionActivity() {
      return sessionActivity;
    },
    get refreshToken() {
      return `${cronRefreshToken}:${sessionsRefreshToken}`;
    },
    get sessionInvalidations() {
      return sessionInvalidations;
    },
    get connection() {
      return connectionSnapshot;
    },
    get unavailable() {
      return serverUnavailable;
    },
  });
  $effect(() => {
    navigationIndicators.cron =
      schedules.count > 0
        ? {
            count: schedules.count,
            tone: 'unread',
            label: t('cron.runs.unread', { count: schedules.count }),
          }
        : null;
  });
  let serverSwitcherOpen = $state(false);

  $effect(() => {
    if (!serverUnavailable) {
      serverSwitcherOpen = false;
    }
  });

  // Every navigation - a click, a deep link, Back or Forward - waits for
  // pending edits here. Leaving Settings also keeps its reading position:
  // captured while Settings still owns its DOM, because scroll events alone
  // must not decide the navigation boundary.
  const navigationGate = (action) =>
    autosave.requestTransition(() => {
      if (activeViewId === 'settings') {
        const position = settingsView?.getScrollPosition?.();
        if (position) {
          settingsScrollPosition = position;
        }
      }
      return action();
    });

  // A Desktop restart into a new version replaces this page. The Restart
  // button and the Desktop's request both come here. The user's restart saves
  // pending edits through the navigation's autosave gate (with its Retry /
  // Discard dialog) and asks before it discards an unsaved new Cron job. The
  // Desktop's request follows an update the user started and is never
  // declined: it saves pending edits for at most the time a navigation waits
  // before it offers to leave, then restarts without any UI (an unsaved new
  // Cron job is discarded, a Live voice call ends).
  const restartDesktopApp = async ({
    interactive = false,
    discardCronDraft = false,
  } = {}) => {
    const restart = () => {
      // Unsent Chat composer text survives in the new window.
      flushComposerMemory();
      return desktop.requestRestart({ interactive });
    };
    if (interactive) {
      if (cronNewJobDraft && !discardCronDraft) {
        restartDiscardConfirmOpen = true;
        return false;
      }
      return autosave.requestTransition(restart);
    }
    // A slower save keeps running and may still land before the handoff.
    if (autosave.hasPending()) await autosave.flushWithinLimit();
    return restart();
  };

  const confirmRestartDiscardingCronDraft = () => {
    restartDiscardConfirmOpen = false;
    void restartDesktopApp({ interactive: true, discardCronDraft: true });
  };

  const desktopAccessor = isDesktopAccessor();
  const navigator = createNavigator({
    defaultView: navigationItems[0].id,
    isKnownView,
    gate: navigationGate,
    guardExit: desktopAccessor,
    handleInput: desktopAccessor,
  });
  provideNavigation(navigator);
  let activeViewId = $derived(navigator.location.view);
  const chatNavigation = createAppChatNavigation({
    navigator,
    selection,
    openChat: () => openView('chat'),
  });

  // The main navigation: a view's last place; the view already shown stays
  // where it is. Leaving first-run setup for another view sets setup aside.
  const openView = (viewId) => {
    if (viewId !== activeViewId && !setup.operational) {
      setup.dismissOnboarding();
    }
    return navigator.open(viewId);
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

  // Open chat on an Agent row: Chat shows that Agent, at its last place when
  // the Agent was already selected, else at its current Session.
  const openAgentChat = (agentId) => {
    if (!agentId) return false;
    selectAgentFromView(agentId);
    return openView('chat');
  };

  const liveUiActions = createLiveUiActions({
    selection,
    navigator,
    navigateToSession: chatNavigation.navigateToSession,
    activeView: () => activeViewId,
    requestTransition: autosave.requestTransition,
    chatSelection: () => chatNavigation.shownSession,
    terminalsView: () => terminalsView,
    afterRender: async () => {
      // A voice layout request can be the first opening of Terminals.
      // Wait for its code as well as the render that binds its controller.
      await loadComponent(loadTerminalsView);
      await tick();
    },
  });

  const acknowledgeDataStoreRecovery = async () => {
    try {
      await appController.acknowledgeDataStoreRecovery();
    } catch (error) {
      desktop.showToast({
        title: t('dataStore.acknowledgeFailedTitle'),
        message: error?.message ?? t('dataStore.acknowledgeFailed'),
        variant: 'error',
      });
    }
  };

  // Every (re)connection may follow an update that replaced the served WebUI.
  const checkWebuiBuild = async () => {
    try {
      appControllerState.webuiOutdated =
        await isWebuiOutdated(getServedWebuiBuild);
    } catch {
      // Keep the last answer when the served build cannot be read.
    }
  };

  const connectServerEvents = () => appController.connectServerEvents();

  const navigateToAgentModel = () =>
    navigator.navigate('agents', [selection.selectedAgentId]);

  const navigateToProjects = () => openView('projects');
  const saveProjectOrder = (projectIds) =>
    selection.saveProjectOrder(projectIds).catch((error) => {
      desktop.showToast({
        title: t('projects.order.saveError'),
        message: error?.message,
        variant: 'error',
      });
    });

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

  // Deep-link to one Skill's page in the Skills manager, seen from an Agent.
  const navigateToAgentSkill = (agentId, skillId) =>
    navigator.navigate('skills', [`agent:${agentId}`, skillId]);

  const navigateToProjectSkill = (projectId, skillId) =>
    navigator.navigate('skills', [`project:${projectId}`, skillId]);

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
      renameComposerAgent(oldAgentId, newAgentId);
      renameWidgetStatesAgent(oldAgentId, newAgentId);
      speechAudio.renameAgent(oldAgentId, newAgentId);
      // Chat areas re-key their Sessions here; the place Chat's first area
      // reported follows, so the remapped place reads as already shown.
      selection.remapIdentityAgentId(oldAgentId, newAgentId);
      chatNavigation.remapIdentityAgentId(oldAgentId, newAgentId);
    },
    onReloadAgents: selection.reloadAgentsFromServer,
    onReloadExtensionPages: extensions.loadExtensionPages,
    onExtensionChange: extensions.publishChange,
    onPortalsChanged: invalidatePortals,
    onCheckWebuiBuild: checkWebuiBuild,
    onSpeechEvent: speechAudio.handleServerEvent,
  });
  const runEvents = appController.runEvents;
  navigator.start();

  onMount(() => {
    const stopClientMetrics = startClientMetrics({
      report: reportClientMetrics,
    });
    const sessionLink = navigator.takeSessionLink();
    if (sessionLink) {
      chatNavigation.openSessionLink(sessionLink);
    }
    const stopDesktopSessionRequests = isDesktopAccessor()
      ? onDesktopOpenSession(chatNavigation.openSessionLink)
      : () => {};
    // This page always takes over the Desktop's restart request, so it saves
    // pending edits before the Desktop would restart on its own.
    const stopDesktopRestartRequests = isDesktopAccessor()
      ? onDesktopRestartRequest(() => {
          void restartDesktopApp();
        })
      : () => {};
    connectServerEvents();

    const onVisibilityChange = () => {
      handleVisibilityChange(appControllerState.connectionState);
    };
    document.addEventListener('visibilitychange', onVisibilityChange);

    // Load the project list for the chat dropdown (best-effort; the chat works
    // identity-only when this fails).
    void selection.loadProjects().finally(chatNavigation.settleInitialProjects);
    // Voice routing and other non-Chat views also consume the shared Agent
    // roster, so seed it at app mount instead of relying on ChatView having
    // mounted first.
    void selection.seedAgentsFromServer();

    // Seed app-wide appearance preferences and the operational state that
    // drives first-run onboarding. Chat appearance preferences are passed to
    // ChatView; the language seed closes the startup-language gap.
    void setup.loadAppSettings();

    return () => {
      autosave.destroy();
      stopDesktopSessionRequests();
      stopDesktopRestartRequests();
      selection.destroy();
      document.removeEventListener('visibilitychange', onVisibilityChange);
      appController.destroy();
      navigator.destroy();
      stopClientMetrics();
    };
  });
  // Unsaved edits and an unsaved new Cron job ask before the page unloads.
  // A Desktop restart in progress already asked about the Cron job.
  function protectPendingEdits(event) {
    const cronDraftAtRisk =
      Boolean(cronNewJobDraft) && !desktop.update?.restarting;
    if (!autosave.hasPending() && !cronDraftAtRisk) return;
    event.preventDefault();
    event.returnValue = '';
  }

  const desktopRestartBusy = $derived(
    Boolean(desktop.update?.restarting) || desktop.restartRequesting,
  );
</script>

<svelte:window onbeforeunload={protectPendingEdits} />

<AppShell
  items={visibleNavigationItems}
  indicators={navigationIndicators}
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
  {#if desktop.restartOffered}
    <!-- A Desktop restart also loads the new WebUI, so it replaces the
         reload banner. -->
    <Banner variant="info" class="app-desktop-restart">
      <span class="app-desktop-restart__text">
        {desktop.update?.failed && !desktopRestartBusy
          ? t('app.desktopRestart.failed')
          : t('app.desktopRestart.pending')}
      </span>
      <Button
        variant="secondary"
        loading={desktopRestartBusy}
        onClick={() => restartDesktopApp({ interactive: true })}
      >
        {#if desktopRestartBusy}
          {t('app.desktopRestart.restarting')}
        {:else if desktop.update?.failed}
          {t('common.retry')}
        {:else}
          {t('app.desktopRestart.restart')}
        {/if}
      </Button>
    </Banner>
  {:else if webuiOutdated}
    <Banner variant="info" class="app-webui-outdated">
      <span class="app-webui-outdated__text">
        {t('app.webuiOutdated')}
      </span>
      <Button variant="secondary" onClick={() => window.location.reload()}>
        {t('app.webuiOutdatedReload')}
      </Button>
    </Banner>
  {/if}
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
  {#if speechAudio.active && (activeViewId !== 'chat' || chatNavigation.shownSession?.agentId !== speechAudio.active.agentId || chatNavigation.shownSession?.sessionId !== speechAudio.active.sessionId)}
    <SpeechPlaybackBar
      owner={speechAudio}
      onOpenSession={(entry) => chatNavigation.openSessionLink(entry)}
    />
  {/if}
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
      <LazyContent load={loadOnboardingView}>
        {#snippet children(Component)}
          <Component
            {providerAuthEvent}
            {modelsRefreshToken}
            targetAgentId={selection.selectedAgentId || 'main'}
            onComplete={setup.completeOnboarding}
            onDismiss={setup.dismissOnboarding}
            onToast={desktop.showToast}
          />
        {/snippet}
      </LazyContent>
    {:else}
      <ChatWorkspace
        onToast={desktop.showToast}
        active={activeViewId === 'chat'}
        sharedAgents={selection.agents}
        sharedSelectedAgentId={selection.selectedAgentId}
        chatWidth={appearancePrefs.chatWidth}
        chatWorkingMode={appearancePrefs.chatWorkingMode}
        projects={selection.projects}
        projectsLoaded={selection.projectsLoaded}
        selectedProjectId={selection.selectedProjectId}
        onProjectSelected={selection.selectProject}
        sharedSelectedProjectAgentId={selection.selectedProjectAgentId}
        onProjectAgentSelected={selection.selectProjectAgent}
        onNavigateToProjects={navigateToProjects}
        onReorderProjects={saveProjectOrder}
        agentsRefreshToken={selection.agentsRefreshToken}
        onAgentsChanged={selection.syncAgents}
        onAgentSelected={selection.selectAgent}
        navigateToSubAgent={chatNavigation.navigateToSubAgent}
        pendingSessionNavigation={chatNavigation.pendingSessionNavigation}
        onSessionNavigation={chatNavigation.handleSessionNavigation}
        {runEvents}
        {commandStatuses}
        {sessionActivity}
        {terminalsRefreshToken}
        {agentSecretsRefreshToken}
        {connectionSnapshot}
        {sessionsRefreshToken}
        {sessionInvalidations}
        {commandsRefreshToken}
        {queueInvalidation}
        {sessionDeletion}
        splitRequest={chatNavigation.splitRequest}
        subscribeAgentRenames={selection.subscribeAgentRenames}
        subscribeExtensionInvalidations={extensions.subscribeInvalidations}
        hasConnectedProvider={setup.settings === null
          ? null
          : setup.operational}
        onConnectProvider={navigateToProviders}
        onPickModel={navigateToAgentModel}
        onNavigationIndicator={(indicator) =>
          (navigationIndicators.chat = indicator)}
      />
      {#if activeViewId.startsWith('extension:')}
        {@const page = extensions.extensionPages.find(
          (item) => item.route === activeViewId,
        )}
        <LazyContent load={loadExtensionPage}>
          {#snippet children(Component)}
            <Component
              descriptor={page}
              navigation={navigator.view(activeViewId)}
              theme={{ ...extensions.extensionPageTheme }}
              locale={setup.settings?.appearance?.language ?? 'en'}
              timezone={dateTimePrefs.timeZone}
              subscribeInvalidations={extensions.subscribeInvalidations}
              onToast={(message, variant) =>
                desktop.showToast({ title: message, variant })}
            />
          {/snippet}
        </LazyContent>
      {:else if activeViewId === 'agents'}
        <LazyContent load={loadAgentsView}>
          {#snippet children(Component)}
            <Component
              navigation={navigator.view('agents')}
              sharedSelectedAgentId={selection.selectedAgentId}
              onAgentsChanged={selection.refreshAgents}
              onAgentSelected={selectAgentFromView}
              onOpenChat={openAgentChat}
              onOpenInSplit={chatNavigation.openAgentInSplit}
              onToast={desktop.showToast}
              onNavigateToSettingsPanel={navigateToSettingsPanel}
              onNavigateToAgentPrompt={navigateToAgentPromptScope}
              onOpenSkill={navigateToAgentSkill}
              subscribeAgentRenames={selection.subscribeAgentRenames}
              agentsRefreshToken={selection.agentsRefreshToken}
              {memoriesRefreshToken}
              {modelsRefreshToken}
              {projectsRefreshToken}
              {skillsRefreshToken}
            />
          {/snippet}
        </LazyContent>
      {:else if activeViewId === 'terminals'}
        <LazyContent load={loadTerminalsView}>
          {#snippet children(Component)}
            <Component
              bind:this={terminalsView}
              navigation={navigator.view('terminals')}
              {terminalsRefreshToken}
              {serverUnavailable}
              onToast={desktop.showToast}
              onOpenSession={chatNavigation.navigateToSession}
            />
          {/snippet}
        </LazyContent>
      {:else if activeViewId === 'projects'}
        <LazyContent load={loadProjectsView}>
          {#snippet children(Component)}
            <Component
              navigation={navigator.view('projects')}
              selectedProjectId={selection.managedProjectId}
              {memoriesRefreshToken}
              onProjectSelected={selection.selectManagedProject}
              onToast={desktop.showToast}
              onNavigateToSettingsPanel={navigateToSettingsPanel}
              onOpenSkill={navigateToProjectSkill}
              agentsRefreshToken={selection.agentsRefreshToken}
              {modelsRefreshToken}
              {projectsRefreshToken}
              {skillsRefreshToken}
            />
          {/snippet}
        </LazyContent>
      {:else if activeViewId === 'calendar'}
        <LazyContent load={loadCalendarView}>
          {#snippet children(Component)}
            <Component
              navigation={navigator.view('calendar')}
              onToast={desktop.showToast}
              {serverUnavailable}
              {calendarRefreshToken}
              {cronRefreshToken}
              agentsRefreshToken={selection.agentsRefreshToken}
              onOpenCronJob={openCronJobFromCalendar}
              onOpenSession={chatNavigation.navigateToSession}
            />
          {/snippet}
        </LazyContent>
      {:else if activeViewId === 'cron'}
        <LazyContent load={loadCronView}>
          {#snippet children(Component)}
            <Component
              navigation={navigator.view('cron')}
              bind:newJobDraft={cronNewJobDraft}
              scheduleActivity={schedules.jobs}
              activityError={schedules.error}
              onRefreshActivity={schedules.refresh}
              onOpenSession={chatNavigation.navigateToSession}
              onToast={desktop.showToast}
              {serverUnavailable}
              {cronRefreshToken}
              agentsRefreshToken={selection.agentsRefreshToken}
              {projectsRefreshToken}
            />
          {/snippet}
        </LazyContent>
      {:else if activeViewId === 'skills'}
        <LazyContent load={loadSkillsView}>
          {#snippet children(Component)}
            <Component
              navigation={navigator.view('skills')}
              onToast={desktop.showToast}
              settings={setup.settings}
              onSettingsCommit={(nextSettings) =>
                (setup.settings = nextSettings)}
              onOpenSession={chatNavigation.navigateToSession}
              {skillsRefreshToken}
              agentsRefreshToken={selection.agentsRefreshToken}
              {projectsRefreshToken}
            />
          {/snippet}
        </LazyContent>
      {:else if activeViewId === 'system-prompt'}
        <LazyContent load={loadSystemPromptView}>
          {#snippet children(Component)}
            <Component
              navigation={navigator.view('system-prompt')}
              onToast={desktop.showToast}
              agentsRefreshToken={selection.agentsRefreshToken}
            />
          {/snippet}
        </LazyContent>
      {:else if activeViewId === 'settings'}
        <LazyContent load={loadSettingsView}>
          {#snippet children(Component)}
            <Component
              bind:this={settingsView}
              navigation={navigator.view('settings')}
              onSettingsCommit={(nextSettings) =>
                (setup.settings = nextSettings)}
              onNavigateToAgentDefaults={navigateToAgentDefaults}
              {providerAuthEvent}
              onToast={desktop.showToast}
              agents={selection.agents}
              projects={selection.projects}
              desktopCapabilities={desktop.desktopCapabilities}
              desktopVoice={desktop.desktopVoice}
              desktopZoom={desktop.desktopZoom}
              {debugTracesRefreshToken}
              onOpenSetupGuide={setup.reopenOnboarding}
              {modelsRefreshToken}
              {recallIndexStatus}
              {backgroundActivity}
              {secretStore}
              {clientsRefreshToken}
              {devicesRefreshToken}
              {credentialsRefreshToken}
              {agentSecretsRefreshToken}
              {channelsRefreshToken}
              {archiveRefreshToken}
              agentsRefreshToken={selection.agentsRefreshToken}
              {projectsRefreshToken}
              {sessionsRefreshToken}
              {skillsRefreshToken}
              onOpenSession={chatNavigation.navigateToSession}
              initialScrollPosition={settingsScrollPosition}
              onScrollPositionChange={rememberSettingsScrollPosition}
              subscribeExtensionInvalidations={extensions.subscribeInvalidations}
            />
          {/snippet}
        </LazyContent>
      {:else if activeViewId === 'logs'}
        <LazyContent load={loadLogsView}>
          {#snippet children(Component)}
            <Component navigation={navigator.view('logs')} />
          {/snippet}
        </LazyContent>
      {:else if activeViewId === 'statistics'}
        <LazyContent load={loadStatisticsView}>
          {#snippet children(Component)}
            <Component navigation={navigator.view('statistics')} />
          {/snippet}
        </LazyContent>
      {/if}
    {/if}
  {/key}
  <ToastStack
    toasts={desktop.toastState.toasts}
    onDismiss={desktop.dismissAppToast}
  />
</AppShell>

<AutosaveTransitionPrompt transition={autosave} />

{#if restartDiscardConfirmOpen}
  <ConfirmDialog
    title={t('app.desktopRestart.cronDraftTitle')}
    body={t('app.desktopRestart.cronDraftBody')}
    confirmLabel={t('app.desktopRestart.cronDraftConfirm')}
    onConfirm={confirmRestartDiscardingCronDraft}
    onCancel={() => (restartDiscardConfirmOpen = false)}
  />
{/if}

{#if serverSwitcherOpen}
  <Modal
    title={t('settings.desktop.switchModalTitle')}
    labelledById="desktop-server-switch-title"
    size="lg"
    onClose={() => (serverSwitcherOpen = false)}
  >
    {#snippet body()}
      <div class="modal-body">
        <LazyContent load={loadDesktopConnectionSettings}>
          {#snippet children(Component)}
            <Component
              idPrefix="desktop-outage-server"
              onToast={desktop.showToast}
            />
          {/snippet}
        </LazyContent>
      </div>
    {/snippet}
  </Modal>
{/if}

<style>
  /* Slim re-entry banner for a dismissed-but-incomplete first-run setup. Sits
     above the active view inside the content column and disappears the instant
     a provider is connected. */
  :global(.app-finish-setup),
  :global(.app-webui-outdated),
  :global(.app-desktop-restart) {
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

  .app-finish-setup__text,
  .app-webui-outdated__text,
  .app-desktop-restart__text {
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
  }

  @media (max-width: 640px) {
    :global(.app-finish-setup),
    :global(.app-webui-outdated),
    :global(.app-desktop-restart) {
      padding: 8px 14px;
    }

    :global(.app-data-store-incident) {
      align-items: stretch;
      padding: 10px 14px;
    }
  }
</style>
