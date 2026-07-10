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
    sameNavigationSelection,
    sameSessionOverride,
    viewIdFromLocationHash,
  } from '$lib/navigationHistory.js';
  import { createToastState, addToast, dismissToast } from '$lib/toastState.js';
  import { isOperational } from '$lib/onboarding.js';
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
  const SELECTED_PROJECT_AGENT_KEY = 'vbot.selectedProjectAgentId';
  // Accessor-local UI state only: whether the user set the first-run wizard
  // aside this browser. The real trigger stays the live operational state — a
  // credential removal clears this flag and brings the wizard back on its own.
  const ONBOARDING_DISMISSED_KEY = 'vbot.onboardingDismissed';
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

  let activeViewId = $state(initialViewId());
  let debugEnabled = $state(false);
  let agents = $state([]);
  let selectedAgentId = $state(readStoredSelectedAgentId());
  // Project context for the two-bar chat. `projects` feeds the chat dropdown;
  // `selectedProjectId` is the chosen project (empty = Personal/identity path).
  let projects = $state([]);
  let selectedProjectId = $state(readStoredSelectedProjectId());
  // The remembered active agent inside the selected project (tri-state: null =
  // nothing remembered, '' = identity agent active alongside the project, or a
  // bare team-member id). Persisted like the selected agent/project; ChatView
  // reports changes back through `onProjectAgentSelected`.
  let selectedProjectAgentId = $state(readStoredSelectedProjectAgentId());
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
  // Application settings, fetched on mount and re-fetched on a provider/model
  // change. Drives the first-run onboarding decision. Null until first loaded.
  let settings = $state(null);
  let onboardingDismissed = $state(readOnboardingDismissed());
  // Sticky once shown: the wizard stays until completed/dismissed, so the
  // connect flip (operational → true) never yanks it before the model step.
  let onboardingActive = $state(false);
  let lastSettingsModelsToken = null;
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
  // System Prompt scope deep-link target (an agent id) + a fresh request id per
  // request, so SystemPromptView selects that agent's scope when the id changes.
  let promptScopeTarget = $state('');
  let promptScopeTargetRequestId = $state(0);
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
      const result = await rpc('settings.get');
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

  const pushNavigationState = () => {
    try {
      history.pushState(
        createNavigationHistoryState(
          activeViewId,
          chatSessionOverride,
          currentNavigationSelection(),
        ),
        '',
        locationHashForView(activeViewId),
      );
    } catch {
      // History API unavailable (non-browser environment)
    }
  };

  const selectView = (viewId) => {
    // Navigating away while vBot is not operational sets the guided setup aside
    // (the rest of the app stays reachable), leaving the "Finish setup" banner
    // as the re-entry. This also blocks a late settings response from popping
    // the wizard over the view the user just opened.
    if (viewId !== activeViewId && !operational) {
      onboardingActive = false;
      onboardingDismissed = true;
      writeOnboardingDismissed(true);
    }
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
    // Entries written before the selection field existed (or foreign states)
    // carry no selection; they restore only the session override, as before.
    const selection = navState.selection ?? null;
    const selectionDiffers =
      selection &&
      !sameNavigationSelection(selection, currentNavigationSelection());
    if (!selectionDiffers && sameSessionOverride(chatSessionOverride, target)) {
      return;
    }
    chatSessionOverride = target;
    sessionNavigationRequestId += 1;
    // ChatView applies the selection itself and reports it back through the
    // normal non-pushing callbacks (`onAgentSelected`/`onProjectSelected`/
    // `onProjectAgentSelected`), so App's mirrors converge without this
    // restore ever echoing into a new history push.
    pendingSessionNavigation = {
      ...(target ? { ...target } : { returnToCurrent: true }),
      requestId: sessionNavigationRequestId,
      selection,
    };
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
    autoDismiss,
  }) => {
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

  const handleServerEvent = async (event) => {
    if (event.type === 'app_error') {
      // Error toasts stay until dismissed by default (see showToast) — a
      // transport/server failure the user must acknowledge.
      showToast({
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

  // Deep-link to a specific Settings panel (Agent defaults, Extensions, Voice…).
  // Sets the target + a fresh request id, then switches to the Settings view;
  // SettingsView selects the panel when the request id changes.
  const navigateToSettingsPanel = (panelId) => {
    settingsPanelTarget = panelId;
    settingsPanelTargetRequestId += 1;
    selectView('settings');
  };

  const navigateToVoiceSettings = () => {
    navigateToSettingsPanel('voice');
  };

  // Deep-link to the System Prompt view with a given agent's scope preselected.
  // Mirrors the settings-panel mechanism: a target agent id + a fresh request id
  // SystemPromptView reacts to once scopes have loaded, falling back to the
  // default scope when the target scope is absent.
  const navigateToAgentPromptScope = (agentId) => {
    promptScopeTarget = typeof agentId === 'string' ? agentId : '';
    promptScopeTargetRequestId += 1;
    selectView('system-prompt');
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
      const existingState = isNavigationHistoryState(history.state)
        ? history.state
        : null;
      if (
        existingState &&
        existingState.view === activeViewId &&
        existingState.session
      ) {
        // Reload with a session override on top of the history stack: adopt
        // the entry instead of overwriting it, so the override survives the
        // reload and Back/Forward keep working from it. The selection half is
        // already restored through the localStorage mirrors — they were
        // persisted by the same state that pushed this entry.
        chatSessionOverride = existingState.session;
        sessionNavigationRequestId += 1;
        pendingSessionNavigation = {
          ...existingState.session,
          requestId: sessionNavigationRequestId,
        };
      } else {
        // Seed the current history entry so Back can always restore it; later
        // navigation pushes new entries on top.
        history.replaceState(
          createNavigationHistoryState(
            activeViewId,
            null,
            currentNavigationSelection(),
          ),
          '',
          locationHashForView(activeViewId),
        );
      }
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

    // Seed app-wide appearance preferences and the operational state that
    // drives first-run onboarding. `chat_width` drives the chat reading-column
    // width app-wide (passed to ChatView); the language seed closes the
    // startup-language gap.
    void loadAppSettings();

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
    />
  {:else if activeViewId === 'projects'}
    <ProjectsView
      onToast={showToast}
      onNavigateToSettingsPanel={navigateToSettingsPanel}
      {modelsRefreshToken}
    />
  {:else if activeViewId === 'cron'}
    <CronView onToast={showToast} />
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
