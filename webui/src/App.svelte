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
  import ConfirmDialog from './components/ui/ConfirmDialog.svelte';
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
    getServedWebuiBuild,
    listBackgroundActivity,
    reportClientMetrics,
  } from '$lib/api.js';
  import { startClientMetrics } from '$lib/clientMetrics.js';
  import { isWebuiOutdated } from '$lib/webuiBuild.js';
  import {
    AUTOSAVE_STILL_SAVING_MS,
    createAutosaveCoordinator,
    provideAutosaveContext,
  } from '$lib/autosave.js';
  import { flushComposerMemory } from '$lib/composerMemory.js';
  import {
    isDesktopAccessor,
    onDesktopOpenSession,
    onDesktopRestartRequest,
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

  // A navigation waits here while pending edits save. `autosavePrompt` is
  // null, 'slow' (a save takes long: Keep waiting / Leave anyway) or
  // 'failed' (Retry / Discard and continue).
  let autosaveTransitionSaving = $state(false);
  let autosavePrompt = $state(null);
  let pendingAutosaveTransition = null;
  // Each flush belongs to one transition; one the user left behind cannot
  // navigate or reopen a prompt when it settles.
  let autosaveTransitionId = 0;
  let autosaveTransitionSlow = false;
  let autosaveSlowTimer = null;
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
  let restartDiscardConfirmOpen = $state(false);

  let modelsRefreshToken = $derived(appControllerState.modelsRefreshToken);
  let recallIndexStatus = $derived(appControllerState.recallIndexStatus);
  let backgroundActivity = $derived(appControllerState.backgroundActivity);
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
  let commandStatuses = $derived(appControllerState.commandStatuses);
  let connectionSnapshot = $derived(appControllerState.connectionSnapshot);
  let activeRuns = $derived(appControllerState.activeRuns);

  let serverSwitcherOpen = $state(false);

  $effect(() => {
    if (!serverUnavailable) {
      serverSwitcherOpen = false;
    }
  });

  const stopAutosaveSlowTimer = () => {
    clearTimeout(autosaveSlowTimer);
    autosaveSlowTimer = null;
    autosaveTransitionSlow = false;
  };

  // Ends the waiting transition without running it; a flush still running
  // for it settles unobserved.
  const closeAutosaveTransition = () => {
    autosaveTransitionId += 1;
    stopAutosaveSlowTimer();
    autosaveTransitionSaving = false;
    autosavePrompt = null;
    pendingAutosaveTransition = null;
  };

  const runAutosaveTransition = async (action) => {
    const transitionId = ++autosaveTransitionId;
    pendingAutosaveTransition = action;
    autosaveTransitionSaving = true;
    stopAutosaveSlowTimer();
    // A save that takes long offers to leave; an open failure prompt (a
    // Retry) already offers it.
    autosaveSlowTimer = setTimeout(() => {
      autosaveSlowTimer = null;
      if (transitionId !== autosaveTransitionId) return;
      autosaveTransitionSlow = true;
      autosavePrompt ??= 'slow';
    }, AUTOSAVE_STILL_SAVING_MS);
    const saved = await autosaveCoordinator.flushPending();
    if (transitionId !== autosaveTransitionId) return false;
    stopAutosaveSlowTimer();
    autosaveTransitionSaving = false;

    if (!saved) {
      autosavePrompt = 'failed';
      return false;
    }

    const latestAction = pendingAutosaveTransition;
    pendingAutosaveTransition = null;
    autosavePrompt = null;
    return latestAction?.();
  };

  const requestAutosaveTransition = (action) => {
    if (typeof action !== 'function' || autosavePrompt === 'failed') {
      return false;
    }
    if (autosaveTransitionSaving) {
      pendingAutosaveTransition = action;
      // The save already took long: ask again instead of waiting silently.
      if (autosaveTransitionSlow) autosavePrompt ??= 'slow';
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

  // Keep waiting: the navigation still runs once the save finishes.
  const keepWaitingForAutosave = () => {
    if (autosavePrompt === 'slow') autosavePrompt = null;
  };

  // Leave anyway / Discard and continue: the latest navigation runs now,
  // also while a save still runs. Running saves finish in the background.
  const leaveAutosaveTransition = () => {
    const action = pendingAutosaveTransition;
    if (!action) return;
    closeAutosaveTransition();
    autosaveCoordinator.releaseRunningSaves();
    action();
  };

  // Closing the failure prompt stays with the unsaved edits: the navigation
  // is dropped, and a pending Back/Forward returns to the shown place.
  const stayAfterAutosaveFailure = () => {
    if (autosavePrompt !== 'failed') return;
    closeAutosaveTransition();
    navigator.cancelPendingMove();
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

  // Whether pending edits saved within the time a navigation waits before it
  // offers to leave. A slower save keeps running.
  const flushPendingWithinStillSavingLimit = () => {
    let timer;
    const limit = new Promise((resolve) => {
      timer = setTimeout(() => resolve(false), AUTOSAVE_STILL_SAVING_MS);
    });
    return Promise.race([autosaveCoordinator.flushPending(), limit]).finally(
      () => clearTimeout(timer),
    );
  };

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
      return requestAutosaveTransition(restart);
    }
    // A slower save keeps running and may still land before the handoff.
    if (autosaveCoordinator.hasPending())
      await flushPendingWithinStillSavingLimit();
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
    resolveView: (viewId) =>
      viewId === 'debug' && debugEnabled === false ? 'settings' : viewId,
    gate: navigationGate,
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
      // A place naming only an Agent is that Agent's unsaved draft.
      pendingSessionNavigation = {
        ...(agentId && sessionId
          ? {
              agentId,
              sessionId,
              subAgent,
              followSession: subAgent && location.origin === 'app',
            }
          : agentId
            ? { agentId, draft: true }
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
  // sessionId, subAgent}`; no `sessionId` for a draft, whose place names only
  // the Agent). A report while Chat is hidden corrects the place Chat returns
  // to and never pulls the app back to Chat.
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

  // Open chat on an Agent row: Chat shows that Agent, at its last place when
  // the Agent was already selected, else at its current Session.
  const openAgentChat = (agentId) => {
    if (!agentId) return false;
    selectAgentFromView(agentId);
    return openView('chat');
  };

  // A remembered Location with an Identity Agent's old id replaced by its
  // new one.
  function renameAgentInLocation(location, oldAgentId, newAgentId) {
    const resolve = (agentId) =>
      agentId === oldAgentId ? newAgentId : agentId;
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

  const loadBackgroundActivity = async () => {
    try {
      const result = await listBackgroundActivity();
      appControllerState.backgroundActivity = Array.isArray(result?.activities)
        ? result.activities
        : [];
    } catch {
      // The next `activity_status` push or connection brings the list.
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
      navigator.remapAll((location) =>
        renameAgentInLocation(location, oldAgentId, newAgentId),
      );
    },
    onReloadAgents: selection.reloadAgentsFromServer,
    onReloadExtensionPages: extensions.loadExtensionPages,
    onExtensionChange: extensions.publishChange,
    onLoadDataStoreStatus: loadDataStoreStatus,
    onLoadBackgroundActivity: loadBackgroundActivity,
    onCheckWebuiBuild: checkWebuiBuild,
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
      closeAutosaveTransition();
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
    if (!autosaveCoordinator.hasPending() && !cronDraftAtRisk) return;
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
        projectsLoaded={selection.projectsLoaded}
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
        {commandStatuses}
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
          onOpenChat={openAgentChat}
          onToast={desktop.showToast}
          onNavigateToSettingsPanel={navigateToSettingsPanel}
          onNavigateToAgentPrompt={navigateToAgentPromptScope}
          onOpenSkill={navigateToAgentSkill}
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
          onOpenSession={navigateToSession}
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
          projects={selection.projects}
          desktopCapabilities={desktop.desktopCapabilities}
          desktopVoice={desktop.desktopVoice}
          onDebugEnabledChange={handleDebugEnabledChange}
          onOpenSetupGuide={setup.reopenOnboarding}
          {modelsRefreshToken}
          {recallIndexStatus}
          {backgroundActivity}
          {clientsRefreshToken}
          {channelsRefreshToken}
          {archiveRefreshToken}
          agentsRefreshToken={selection.agentsRefreshToken}
          {projectsRefreshToken}
          {sessionsRefreshToken}
          {skillsRefreshToken}
          onOpenSession={navigateToSession}
          initialScrollPosition={settingsScrollPosition}
          onScrollPositionChange={rememberSettingsScrollPosition}
          subscribeExtensionInvalidations={extensions.subscribeInvalidations}
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

{#if autosavePrompt === 'slow'}
  <Modal
    title={t('autosave.stillSavingTitle')}
    labelledById="autosave-transition-slow-title"
    onClose={keepWaitingForAutosave}
  >
    {#snippet body()}
      <div class="modal-body">
        <p>
          {t('autosave.stillSavingBody')}
        </p>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="primary" onClick={keepWaitingForAutosave}>
        {t('autosave.keepWaiting')}
      </Button>
      <Button variant="secondary" onClick={leaveAutosaveTransition}>
        {t('autosave.leaveAnyway')}
      </Button>
    {/snippet}
  </Modal>
{:else if autosavePrompt === 'failed'}
  <!-- Closing it stays with the unsaved edits; Discard stays available
       while a Retry runs, so a hanging save never locks the app. -->
  <Modal
    title={t('autosave.transitionFailureTitle')}
    labelledById="autosave-transition-failure-title"
    onClose={stayAfterAutosaveFailure}
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
      <Button variant="danger" onClick={leaveAutosaveTransition}>
        {t('autosave.discardAndContinue')}
      </Button>
    {/snippet}
  </Modal>
{/if}

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

  :global(.desktop-server-switch-modal) {
    width: min(680px, calc(100vw - 40px));
  }

  .desktop-server-switch-modal__body {
    max-height: min(70vh, 680px);
    overflow-y: auto;
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
