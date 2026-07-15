import {
  CONNECTION_STATUS_CONNECTED,
  CONNECTION_STATUS_DISCONNECTED,
  connect,
  createConnectionState,
  disconnect,
} from './connectionState.js';
import {
  createNavigationHistoryState,
  isNavigationHistoryState,
  locationHashForView,
  sameNavigationSelection,
  sameSessionOverride,
  viewIdFromLocationHash,
} from './navigationHistory.js';
import {
  RESOURCE_TOKEN_AGENTS,
  RESOURCE_TOKEN_CHANNELS,
  RESOURCE_TOKEN_CLIENTS,
  RESOURCE_TOKEN_DEBUG_TRACES,
  RESOURCE_TOKEN_MODELS,
  RESOURCE_TOKEN_PROJECTS,
  RESOURCE_TOKEN_SESSIONS,
  tokenKeysForKind,
} from './resourceInvalidation.js';

const MAX_RUN_SERVER_EVENTS = 500;
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
]);

export function createAppControllerState(activeViewId) {
  return {
    activeViewId,
    channelsRefreshToken: 0,
    clientsRefreshToken: 0,
    connectionSnapshot: null,
    connectionState: createConnectionState(),
    debugTracesRefreshToken: 0,
    modelsRefreshToken: 0,
    pendingSessionNavigation: null,
    projectsRefreshToken: 0,
    promptScopeTarget: '',
    promptScopeTargetRequestId: 0,
    providerAuthEvent: null,
    queueInvalidation: null,
    runServerEvents: [],
    serverNoticeState: '',
    serverRecoveryGeneration: 0,
    sessionsRefreshToken: 0,
    settingsPanelTarget: '',
    settingsPanelTargetRequestId: 0,
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
  onClearOutageErrors,
  onLoadProjects,
  onReloadAgents,
  onSetOnboardingAside,
  browserHistory = globalThis.history,
  browserWindow = globalThis.window,
  unavailableNoticeDelayMs = SERVER_UNAVAILABLE_NOTICE_DELAY_MS,
  restoredNoticeDurationMs = SERVER_RESTORED_NOTICE_DURATION_MS,
}) {
  let chatSessionOverride = null;
  let sessionNavigationRequestId = 0;
  let unavailableNoticeTimer = null;
  let restoredNoticeTimer = null;

  function pushNavigationState() {
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
    let viewId = knownViewIds.includes(navState.view)
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
        knownViewIds,
      ) || defaultViewId;
    applyNavigationState(createNavigationHistoryState(viewId, null));
  }

  function initializeNavigationHistory() {
    try {
      const existingState = isNavigationHistoryState(browserHistory?.state)
        ? browserHistory.state
        : null;
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
      } else {
        browserHistory?.replaceState(
          createNavigationHistoryState(
            state.activeViewId,
            null,
            currentNavigationSelection(),
          ),
          '',
          locationHashForView(state.activeViewId),
        );
      }
    } catch {
      // History API unavailable (non-browser environment).
    }
    browserWindow?.addEventListener?.('popstate', handlePopState);
  }

  function navigateToSubAgent(targetOrAgentId, maybeSessionId) {
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
    handleChatSessionNavigation({ agentId, sessionId, subAgent: true });
    sessionNavigationRequestId += 1;
    state.pendingSessionNavigation = {
      agentId,
      sessionId,
      subAgent: true,
      requestId: sessionNavigationRequestId,
    };
    return true;
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
          onClearOutageErrors();
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
      return;
    }
    if (RUN_SERVER_EVENT_TYPES.has(event.type)) {
      state.runServerEvents = [...state.runServerEvents, event].slice(
        -MAX_RUN_SERVER_EVENTS,
      );
      return;
    }
    if (event.type !== 'resource_changed') {
      return;
    }

    const kind = event.payload?.kind;
    const tokenKeys = tokenKeysForKind(kind);
    if (tokenKeys.includes(RESOURCE_TOKEN_MODELS)) {
      state.modelsRefreshToken += 1;
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_PROJECTS)) {
      state.projectsRefreshToken += 1;
      await onLoadProjects();
    }
    if (tokenKeys.includes(RESOURCE_TOKEN_SESSIONS)) {
      state.sessionsRefreshToken += 1;
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
    if (tokenKeys.includes(RESOURCE_TOKEN_DEBUG_TRACES)) {
      state.debugTracesRefreshToken += 1;
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
    navigateToPromptScope,
    navigateToSettingsPanel,
    navigateToSubAgent,
    selectView,
  };
}
