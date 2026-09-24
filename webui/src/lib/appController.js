import { formatAgentAddress } from './agentAddress.js';
import {
  CONNECTION_REPLAY_STATUS_EPOCH_CHANGED,
  CONNECTION_REPLAY_STATUS_GAP,
  CONNECTION_STATUS_CONNECTED,
  CONNECTION_STATUS_DISCONNECTED,
  connect,
  createConnectionState,
  disconnect,
} from './connectionState.js';
import {
  createNavigationHistoryState,
  isExtensionViewId,
  isNavigationHistoryState,
  locationHashForView,
  requestedViewIdFromLocationHash,
  sameNavigationSelection,
  sameSessionOverride,
  viewIdFromLocationHash,
} from './navigationHistory.js';
import {
  RESOURCE_TOKEN_AGENTS,
  RESOURCE_TOKEN_MEMORIES,
  RESOURCE_TOKEN_CALENDAR,
  RESOURCE_TOKEN_CHANNELS,
  RESOURCE_TOKEN_CLIENTS,
  RESOURCE_TOKEN_CRON,
  RESOURCE_TOKEN_COMMANDS,
  RESOURCE_TOKEN_DEBUG_TRACES,
  RESOURCE_TOKEN_MODELS,
  RESOURCE_TOKEN_PROJECTS,
  RESOURCE_TOKEN_SESSIONS,
  RESOURCE_TOKEN_SKILLS,
  RESOURCE_TOKEN_TERMINALS,
  RESOURCE_KIND_SESSION_STORE,
  tokenKeysForKind,
} from './resourceInvalidation.js';

const MAX_RUN_SERVER_EVENTS = 500;
// One bounded list feeds ChatView's merge; re-applying the whole list is
// idempotent, so no per-event dedup bookkeeping is needed.
const MAX_BACKGROUND_BASH_STATUS_EVENTS = 50;
const CONNECTION_READY_EVENT_TYPE = 'connection_ready';
const SERVER_UNAVAILABLE_NOTICE_DELAY_MS = 1000;
const SERVER_RESTORED_NOTICE_DURATION_MS = 1400;
const SERVER_NOTICE_OFFLINE = 'offline';
const SERVER_NOTICE_RESTORED = 'restored';
const RUN_SERVER_EVENT_TYPES = new Set([
  'run_started',
  'run_output',
  'run_completed',
  'run_cancelled',
  'run_failed',
  'run_interrupted',
]);
const TERMINAL_RUN_SERVER_EVENT_TYPES = new Set([
  'run_completed',
  'run_cancelled',
  'run_failed',
  'run_interrupted',
]);

// The Runs active now: the latest `connection_ready.active_runs`, plus Runs
// started since, minus Runs that ended since. The bounded event window cannot
// provide this for a Chat owner mounted later, because the terminal event of a
// Run the snapshot listed may already have left the window.
function nextActiveRuns(activeRuns, serverEvent) {
  const payload = serverEvent.payload ?? {};
  const runId = payload.run_id;
  if (!runId) {
    return activeRuns;
  }
  const known = activeRuns.some((run) => run?.run_id === runId);
  if (serverEvent.type === 'run_started' && !known) {
    return [...activeRuns, activeRunFromStartedEvent(payload)];
  }
  if (TERMINAL_RUN_SERVER_EVENT_TYPES.has(serverEvent.type) && known) {
    return activeRuns.filter((run) => run?.run_id !== runId);
  }
  return activeRuns;
}

// Same shape as a `connection_ready.active_runs` entry; the stream derives the
// SSE URL and fills controls/iterations from the Run's own events.
function activeRunFromStartedEvent(payload) {
  return {
    run_id: payload.run_id,
    agent_id: payload.agent_id,
    project_id: payload.project_id ?? null,
    session_id: payload.session_id,
    run_kind: payload.run_kind,
    status: 'running',
    started_at: payload.run_event_timestamp,
    ...(payload.contributes_to_agent_activity === false
      ? { contributes_to_agent_activity: false }
      : {}),
    ...(payload.source_session_id
      ? { source_session_id: payload.source_session_id }
      : {}),
  };
}

// A `session.delete` names the archived Session and its landing in the
// `sessions` scope. Every event yields a fresh object so a repeated deletion
// still reaches the Chat areas.
function sessionDeletionFromScope(scope) {
  const agentId = typeof scope?.agent_id === 'string' ? scope.agent_id : '';
  const deletedSessionId =
    typeof scope?.deleted_session_id === 'string'
      ? scope.deleted_session_id
      : '';
  if (!agentId || !deletedSessionId) {
    return null;
  }
  return {
    agentAddress: formatAgentAddress(agentId, scope.project_id),
    deletedSessionId,
    nextSessionId:
      typeof scope.next_session_id === 'string' ? scope.next_session_id : '',
  };
}

export function createAppControllerState(activeViewId) {
  return {
    activeViewId,
    activeRuns: [],
    calendarRefreshToken: 0,
    channelsRefreshToken: 0,
    cronRefreshToken: 0,
    clientsRefreshToken: 0,
    commandsRefreshToken: 0,
    connectionSnapshot: null,
    connectionState: createConnectionState(),
    debugTracesRefreshToken: 0,
    modelsRefreshToken: 0,
    memoriesRefreshToken: 0,
    pendingSessionNavigation: null,
    projectsRefreshToken: 0,
    promptScopeTarget: '',
    promptScopeTargetRequestId: 0,
    providerAuthEvent: null,
    queueInvalidation: null,
    backgroundBashStatusEvents: [],
    runServerEvents: [],
    serverNoticeState: '',
    serverRecoveryGeneration: 0,
    sessionDeletion: null,
    sessionsRefreshToken: 0,
    sessionStoreHealth: null,
    sessionStoreIncident: null,
    settingsPanelTarget: '',
    settingsPanelTargetRequestId: 0,
    skillsRefreshToken: 0,
    terminalsRefreshToken: 0,
  };
}

// The application boundary for navigation, connection lifecycle, server-event
// projection, and global invalidation. App.svelte supplies application actions
// (reload data, show a toast, set onboarding aside) and renders this state.
export function createAppController({
  state,
  knownViewIds,
  defaultViewId,
  currentNavigationSelection,
  isDebugEnabled,
  isOperational,
  onAppError,
  onLoadProjects,
  onAgentIdChanged = () => {},
  onReloadAgents,
  onReloadExtensionPages = async () => {},
  onLoadSessionStoreStatus = async () => {},
  onSetOnboardingAside,
  browserHistory = globalThis.history,
  browserWindow = globalThis.window,
  unavailableNoticeDelayMs = SERVER_UNAVAILABLE_NOTICE_DELAY_MS,
  restoredNoticeDurationMs = SERVER_RESTORED_NOTICE_DURATION_MS,
}) {
  let chatSessionOverride = null;
  let sessionNavigationRequestId = 0;
  // An initial deep link to an Extension page whose route is unknown until
  // the page catalog loads; cleared by the first user navigation.
  let pendingExtensionViewId = '';
  let unavailableNoticeTimer = null;
  let restoredNoticeTimer = null;
  const identityAgentRedirects = new Map();
  const currentKnownViewIds = () =>
    typeof knownViewIds === 'function' ? knownViewIds() : knownViewIds;

  function resolvedIdentityAgentId(agentId) {
    let resolved = agentId;
    const visited = new Set();
    while (
      typeof resolved === 'string' &&
      identityAgentRedirects.has(resolved) &&
      !visited.has(resolved)
    ) {
      visited.add(resolved);
      resolved = identityAgentRedirects.get(resolved);
    }
    return resolved;
  }

  function remapNavigationState(navState) {
    const selection = navState.selection
      ? {
          ...navState.selection,
          agentId: resolvedIdentityAgentId(navState.selection.agentId),
        }
      : null;
    const session = navState.session
      ? {
          ...navState.session,
          agentId: navState.session.projectId
            ? navState.session.agentId
            : resolvedIdentityAgentId(navState.session.agentId),
        }
      : null;
    return { ...navState, selection, session };
  }

  function applyIdentityAgentRename(oldAgentId, newAgentId) {
    for (const [source, target] of identityAgentRedirects) {
      if (target === oldAgentId) {
        identityAgentRedirects.set(source, newAgentId);
      }
    }
    identityAgentRedirects.set(oldAgentId, newAgentId);
    if (chatSessionOverride && !chatSessionOverride.projectId) {
      chatSessionOverride = {
        ...chatSessionOverride,
        agentId: resolvedIdentityAgentId(chatSessionOverride.agentId),
      };
    }
    if (state.pendingSessionNavigation) {
      const pending = state.pendingSessionNavigation;
      state.pendingSessionNavigation = {
        ...pending,
        agentId: pending.projectId
          ? pending.agentId
          : resolvedIdentityAgentId(pending.agentId),
        selection: pending.selection
          ? {
              ...pending.selection,
              agentId: resolvedIdentityAgentId(pending.selection.agentId),
            }
          : pending.selection,
      };
    }
    onAgentIdChanged(oldAgentId, newAgentId);
  }

  function pushNavigationState() {
    pendingExtensionViewId = '';
    try {
      browserHistory?.pushState(
        createNavigationHistoryState(
          state.activeViewId,
          chatSessionOverride,
          currentNavigationSelection(),
        ),
        '',
        locationHashForView(state.activeViewId),
      );
    } catch {
      // History API unavailable (non-browser environment).
    }
  }

  function selectView(viewId) {
    if (viewId !== state.activeViewId && !isOperational()) {
      onSetOnboardingAside();
    }
    if (viewId === state.activeViewId) {
      return false;
    }
    if (state.activeViewId === 'chat') {
      chatSessionOverride = null;
      state.pendingSessionNavigation = null;
    }
    state.activeViewId = viewId;
    pushNavigationState();
    return true;
  }

  function handleChatSessionNavigation(override) {
    const next = override ?? null;
    if (sameSessionOverride(chatSessionOverride, next)) {
      return false;
    }
    chatSessionOverride = next;
    pushNavigationState();
    return true;
  }

  function applyNavigationState(navState) {
    pendingExtensionViewId = '';
    navState = remapNavigationState(navState);
    let viewId = currentKnownViewIds().includes(navState.view)
      ? navState.view
      : defaultViewId;
    if (viewId === 'debug' && !isDebugEnabled()) {
      viewId = 'settings';
    }
    state.activeViewId = viewId;

    if (viewId !== 'chat') {
      chatSessionOverride = null;
      state.pendingSessionNavigation = null;
      return;
    }

    const target = navState.session ?? null;
    const selection = navState.selection ?? null;
    const selectionDiffers =
      selection &&
      !sameNavigationSelection(selection, currentNavigationSelection());
    if (!selectionDiffers && sameSessionOverride(chatSessionOverride, target)) {
      return;
    }
    chatSessionOverride = target;
    sessionNavigationRequestId += 1;
    state.pendingSessionNavigation = {
      ...(target ? { ...target } : { returnToCurrent: true }),
      requestId: sessionNavigationRequestId,
      selection,
    };
  }

  function handlePopState(event) {
    if (isNavigationHistoryState(event.state)) {
      applyNavigationState(event.state);
      return;
    }
    const viewId =
      viewIdFromLocationHash(
        browserWindow?.location?.hash ?? '',
        currentKnownViewIds(),
      ) || defaultViewId;
    applyNavigationState(createNavigationHistoryState(viewId, null));
  }

  function replaceNavigationState() {
    try {
      browserHistory?.replaceState(
        createNavigationHistoryState(
          state.activeViewId,
          null,
          currentNavigationSelection(),
        ),
        '',
        locationHashForView(state.activeViewId),
      );
    } catch {
      // History API unavailable (non-browser environment).
    }
  }

  // The view the page was opened or reloaded with: its hash, else the view of
  // the restored history entry.
  function requestedInitialViewId(existingState) {
    return (
      requestedViewIdFromLocationHash(browserWindow?.location?.hash ?? '') ||
      existingState?.view ||
      ''
    );
  }

  function restoreInitialNavigationEntry() {
    const existingState = isNavigationHistoryState(browserHistory?.state)
      ? browserHistory.state
      : null;
    const requestedViewId = requestedInitialViewId(existingState);
    if (
      requestedViewId !== state.activeViewId &&
      isExtensionViewId(requestedViewId)
    ) {
      if (!currentKnownViewIds().includes(requestedViewId)) {
        // Keep the link's entry and URL, so a reload keeps it too, until the
        // Extension page catalog shows whether the page exists.
        pendingExtensionViewId = requestedViewId;
        return;
      }
      state.activeViewId = requestedViewId;
    }
    if (
      existingState &&
      existingState.view === state.activeViewId &&
      existingState.session
    ) {
      chatSessionOverride = existingState.session;
      sessionNavigationRequestId += 1;
      state.pendingSessionNavigation = {
        ...existingState.session,
        requestId: sessionNavigationRequestId,
      };
      return;
    }
    replaceNavigationState();
  }

  function initializeNavigationHistory() {
    try {
      restoreInitialNavigationEntry();
    } catch {
      // History API unavailable (non-browser environment).
    }
    browserWindow?.addEventListener?.('popstate', handlePopState);
  }

  // Called after each successful Extension page catalog load. Opens a pending
  // initial Extension page link in place of the startup view when the page
  // exists and the user has not navigated meanwhile; when it does not exist,
  // the startup view replaces the link's entry. Returns whether it opened.
  function resolvePendingExtensionView() {
    const viewId = pendingExtensionViewId;
    if (!viewId) {
      return false;
    }
    pendingExtensionViewId = '';
    const available = currentKnownViewIds().includes(viewId);
    if (available) {
      state.activeViewId = viewId;
    }
    replaceNavigationState();
    return available;
  }

  function navigateToSession(
    targetOrAgentId,
    maybeSessionId,
    subAgent = false,
  ) {
    const agentId =
      typeof targetOrAgentId === 'string'
        ? targetOrAgentId
        : (targetOrAgentId?.agentId ?? '');
    const sessionId =
      typeof targetOrAgentId === 'string'
        ? maybeSessionId
        : targetOrAgentId?.sessionId;
    if (!agentId || !sessionId) {
      return false;
    }
    selectView('chat');
    handleChatSessionNavigation({ agentId, sessionId, subAgent });
    sessionNavigationRequestId += 1;
    state.pendingSessionNavigation = {
      agentId,
      sessionId,
      subAgent,
      followSession: true,
      requestId: sessionNavigationRequestId,
    };
    return true;
  }

  function navigateToSubAgent(targetOrAgentId, maybeSessionId) {
    return navigateToSession(targetOrAgentId, maybeSessionId, true);
  }

  function navigateToSettingsPanel(panelId) {
    state.settingsPanelTarget = panelId;
    state.settingsPanelTargetRequestId += 1;
    selectView('settings');
  }

  function navigateToPromptScope(agentId) {
    state.promptScopeTarget = typeof agentId === 'string' ? agentId : '';
    state.promptScopeTargetRequestId += 1;
    selectView('system-prompt');
  }

  function clearConnectionTimers() {
    if (unavailableNoticeTimer) {
      clearTimeout(unavailableNoticeTimer);
      unavailableNoticeTimer = null;
    }
    if (restoredNoticeTimer) {
      clearTimeout(restoredNoticeTimer);
      restoredNoticeTimer = null;
    }
  }

  function handleConnectionStatusChange() {
    const status = state.connectionState.status;
    if (status === CONNECTION_STATUS_DISCONNECTED) {
      if (restoredNoticeTimer) {
        clearTimeout(restoredNoticeTimer);
        restoredNoticeTimer = null;
      }
      if (state.serverNoticeState === SERVER_NOTICE_RESTORED) {
        state.serverNoticeState = '';
      }
      if (
        state.serverNoticeState !== SERVER_NOTICE_OFFLINE &&
        !unavailableNoticeTimer
      ) {
        unavailableNoticeTimer = setTimeout(() => {
          unavailableNoticeTimer = null;
          if (state.connectionState.status !== CONNECTION_STATUS_DISCONNECTED) {
            return;
          }
          state.serverNoticeState = SERVER_NOTICE_OFFLINE;
        }, unavailableNoticeDelayMs);
      }
      return;
    }

    if (unavailableNoticeTimer) {
      clearTimeout(unavailableNoticeTimer);
      unavailableNoticeTimer = null;
    }
    if (
      status === CONNECTION_STATUS_CONNECTED &&
      state.serverNoticeState === SERVER_NOTICE_OFFLINE
    ) {
      state.serverNoticeState = SERVER_NOTICE_RESTORED;
      state.serverRecoveryGeneration += 1;
      restoredNoticeTimer = setTimeout(() => {
        restoredNoticeTimer = null;
        if (state.connectionState.status === CONNECTION_STATUS_CONNECTED) {
          state.serverNoticeState = '';
        }
      }, restoredNoticeDurationMs);
    }
  }

  async function handleServerEvent(event) {
    if (event.type === 'app_error') {
      onAppError(event.payload?.message ?? '');
      return;
    }
    if (event.type === 'provider_auth_completed') {
      state.providerAuthEvent = event;
      return;
    }
    if (event.type === CONNECTION_READY_EVENT_TYPE) {
      state.connectionSnapshot = event;
      state.activeRuns = Array.isArray(event.active_runs)
        ? event.active_runs
        : [];
      const refreshOwners = [onLoadSessionStoreStatus, onReloadExtensionPages];
      if (
        event.replay_status === CONNECTION_REPLAY_STATUS_GAP ||
        event.replay_status === CONNECTION_REPLAY_STATUS_EPOCH_CHANGED
      ) {
        state.modelsRefreshToken += 1;
        state.memoriesRefreshToken += 1;
        state.projectsRefreshToken += 1;
        state.sessionsRefreshToken += 1;
        state.clientsRefreshToken += 1;
        state.channelsRefreshToken += 1;
        state.cronRefreshToken += 1;
        state.calendarRefreshToken += 1;
        state.commandsRefreshToken += 1;
        state.debugTracesRefreshToken += 1;
        state.terminalsRefreshToken += 1;
        state.skillsRefreshToken += 1;
        refreshOwners.push(onLoadProjects, onReloadAgents);
      }
      // Recovery owners are independent. Optional projections cannot hold up
      // resource invalidation or another owner's refresh, even on failure.
      await Promise.all(
        refreshOwners.map((refresh) => Promise.resolve().then(refresh)),
      );
      return;
    }
    if (RUN_SERVER_EVENT_TYPES.has(event.type)) {
      state.runServerEvents = [...state.runServerEvents, event].slice(
        -MAX_RUN_SERVER_EVENTS,
      );
      state.activeRuns = nextActiveRuns(state.activeRuns, event);
      return;
    }
    if (event.type === 'bash_process_status_changed') {
      state.backgroundBashStatusEvents = [
        ...state.backgroundBashStatusEvents,
        event,
      ].slice(-MAX_BACKGROUND_BASH_STATUS_EVENTS);
      return;
    }
    if (event.type !== 'resource_changed') {
      return;
    }

    const kind = event.payload?.kind;
    if (kind === 'extensions') {
      await onReloadExtensionPages();
    }
    if (kind === RESOURCE_KIND_SESSION_STORE) {
      await onLoadSessionStoreStatus();
    }
    if (kind === 'agents') {
      const scope = event.payload?.scope ?? {};
      if (
        typeof scope.old_agent_id === 'string' &&
        scope.old_agent_id &&
        typeof scope.new_agent_id === 'string' &&
        scope.new_agent_id
      ) {
        applyIdentityAgentRename(scope.old_agent_id, scope.new_agent_id);
      }
    }
    const tokenKeys = tokenKeysForKind(kind);
    if (tokenKeys.includes(RESOURCE_TOKEN_MODELS)) {
      state.modelsRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_MEMORIES)) {
      state.memoriesRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_PROJECTS)) {
      state.projectsRefreshToken += 1;
      await onLoadProjects();
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_SESSIONS)) {
      state.sessionsRefreshToken += 1;
      const deletion = sessionDeletionFromScope(event.payload?.scope);
      if (deletion) {
        state.sessionDeletion = deletion;
      }
    }
    if (kind === 'queue') {
      const scope = event.payload?.scope ?? {};
      state.queueInvalidation = {
        agentId: typeof scope.agent_id === 'string' ? scope.agent_id : '',
        sessionId: typeof scope.session_id === 'string' ? scope.session_id : '',
      };
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_CLIENTS)) {
      state.clientsRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_CHANNELS)) {
      state.channelsRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_CRON)) {
      state.cronRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_CALENDAR)) {
      state.calendarRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_COMMANDS)) {
      state.commandsRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_DEBUG_TRACES)) {
      state.debugTracesRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_TERMINALS)) {
      state.terminalsRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_SKILLS)) {
      state.skillsRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_AGENTS)) {
      await onReloadAgents();
    }
  }

  function connectServerEvents() {
    connect(state.connectionState, {
      onEvent: handleServerEvent,
      onStatusChange: handleConnectionStatusChange,
    });
  }

  function destroy() {
    browserWindow?.removeEventListener?.('popstate', handlePopState);
    disconnect(state.connectionState);
    clearConnectionTimers();
  }

  return {
    applyNavigationState,
    connectServerEvents,
    destroy,
    handleChatSessionNavigation,
    handleConnectionStatusChange,
    handlePopState,
    handleServerEvent,
    initializeNavigationHistory,
    resolvePendingExtensionView,
    navigateToPromptScope,
    navigateToSettingsPanel,
    navigateToSubAgent,
    navigateToSession,
    selectView,
  };
}
