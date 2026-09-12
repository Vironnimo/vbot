import {
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_STDERR,
  RUN_EVENT_TOOL_CALL_STDOUT,
} from '../api.js';

export const CHAT_STATUS_IDLE = 'idle';

export const CHAT_STATUS_RUNNING = 'running';

export const CHAT_STATUS_COMPLETED = 'completed';

export const CHAT_STATUS_FAILED = 'failed';

export const CHAT_STATUS_CANCELLED = 'cancelled';

export const CHAT_STATUS_INTERRUPTED = 'interrupted';

export const AGENT_ACTIVITY_IDLE = 'idle';

export const AGENT_ACTIVITY_RUNNING = 'running';

export const AGENT_ACTIVITY_UNREAD = 'unread';

export const TERMINAL_RUN_EVENTS = new Set([
  'run_completed',
  'run_failed',
  'run_cancelled',
  'run_interrupted',
]);

export const TERMINAL_RUN_STATUSES = new Set([
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_CANCELLED,
  CHAT_STATUS_INTERRUPTED,
]);

export const TERMINAL_VISIBLE_DRAFT_EVENT_TYPES = new Set([
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_STDOUT,
  RUN_EVENT_TOOL_CALL_STDERR,
]);

export const isRecord = (value) =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

export function createChatState() {
  return {
    agents: [],
    selectedAgentId: '',
    sessions: {},
    loadingAgents: false,
    agentsError: null,
    loadingHistory: false,
    loadingAgentActivity: false,
    historyError: '',
    agentActivityError: '',
    actionError: '',
    commandsError: '',
    cancellingRun: false,
    availableSkills: [],
    subAgentStatuses: {},
    subAgentResults: {},
    backgroundBashProcesses: {},
  };
}

export function setAgents(
  state,
  agents,
  { preserveSessionSelection = false } = {},
) {
  const previous = new Map(
    state.agents.map((agent) => [agent.id, agent.current_session_id]),
  );
  state.agents = (Array.isArray(agents) ? agents : []).map((agent) =>
    preserveSessionSelection && previous.get(agent.id)
      ? { ...agent, current_session_id: previous.get(agent.id) }
      : agent,
  );
  if (!state.selectedAgentId && state.agents.length > 0) {
    state.selectedAgentId = state.agents[0].id;
  }
  if (
    state.selectedAgentId &&
    !state.agents.some((agent) => agent.id === state.selectedAgentId)
  ) {
    state.selectedAgentId = state.agents[0]?.id ?? '';
  }
  return state.selectedAgentId;
}

export function selectAgent(state, agentId) {
  state.selectedAgentId = agentId;
  return selectedAgent(state);
}

export function selectedAgent(state) {
  return (
    state.agents.find((agent) => agent.id === state.selectedAgentId) ?? null
  );
}

export function sessionKey(agentId, sessionId) {
  return `${agentId}::${sessionId}`;
}

export function ensureSessionState(state, agentId, sessionId) {
  const key = sessionKey(agentId, sessionId);
  if (!state.sessions[key]) {
    state.sessions[key] = {
      key,
      agentId,
      sessionId,
      messages: [],
      historyLoaded: false,
      historySnapshotVersion: 0,
      runEvents: [],
      streamingRunEvents: [],
      streamingPhase: 0,
      seenStreamingEventKeys: new Set(),
      currentRun: null,
      queue: [],
      status: CHAT_STATUS_IDLE,
      error: null,
      actionError: '',
      streamError: '',
      streamStatus: CHAT_STATUS_IDLE,
      usage: null,
      sessionUsage: null,
      contextUsage: null,
      backgroundBashStatuses: {},
      reflectionTasks: {},
      hasOlderHistory: false,
      historyBefore: '',
      loadingOlderHistory: false,
      hasUnreadCompletion: false,
      latestCompletionRunId: '',
      unreadRunId: '',
      unreadRunStatus: '',
      unreadRunAt: '',
      lastActiveAt: '',
      markReadPendingRunId: '',
      markReadFailedRunId: '',
    };
  }
  return state.sessions[key];
}

export function syncAgentSessionActivity(state, agentId, sessions) {
  const rows = (Array.isArray(sessions) ? sessions : []).filter(
    (session) => typeof session?.id === 'string' && session.id.length > 0,
  );
  const listedSessionIds = new Set(rows.map((session) => session.id));
  for (const sessionState of Object.values(state.sessions)) {
    if (
      sessionState.agentId === agentId &&
      !listedSessionIds.has(sessionState.sessionId)
    ) {
      clearSessionCompletionActivity(sessionState);
    }
  }
  for (const row of rows) {
    const sessionState = ensureSessionState(state, agentId, row.id);
    applySessionCompletionActivity(sessionState, row);
  }
  return state;
}

export function applySessionCompletionActivity(sessionState, source) {
  if (!sessionState) {
    return sessionState;
  }
  const lastActiveAt = normalizedActivityText(source?.last_active_at);
  const incomingUnreadAt = normalizedActivityText(source?.unread_run_at);
  const incomingHasUnread = source?.has_unread_completion === true;
  const incomingUnreadRunId = normalizedActivityText(source?.unread_run_id);
  const incomingLatestRunId =
    normalizedActivityText(source?.latest_completion_run_id) ||
    (incomingHasUnread ? incomingUnreadRunId : '');
  const localLatestRunId =
    normalizedActivityText(sessionState.latestCompletionRunId) ||
    (sessionState.hasUnreadCompletion
      ? normalizedActivityText(sessionState.unreadRunId)
      : '');
  if (localLatestRunId && !incomingLatestRunId) {
    return sessionState;
  }
  if (localLatestRunId && incomingLatestRunId) {
    if (localLatestRunId === incomingLatestRunId) {
      if (incomingHasUnread && !sessionState.hasUnreadCompletion) {
        return sessionState;
      }
    } else {
      const localCompletionAt = sessionState.hasUnreadCompletion
        ? sessionState.unreadRunAt
        : sessionState.lastActiveAt;
      const incomingCompletionAt = incomingUnreadAt || lastActiveAt;
      if (
        isAtLeastAsNewActivityTimestamp(localCompletionAt, incomingCompletionAt)
      ) {
        return sessionState;
      }
    }
  }

  sessionState.lastActiveAt = lastActiveAt || sessionState.lastActiveAt;
  sessionState.latestCompletionRunId = incomingLatestRunId;
  sessionState.hasUnreadCompletion = incomingHasUnread;
  sessionState.unreadRunId = incomingHasUnread ? incomingUnreadRunId : '';
  sessionState.unreadRunStatus = incomingHasUnread
    ? normalizedActivityText(source?.unread_run_status)
    : '';
  sessionState.unreadRunAt = incomingHasUnread ? incomingUnreadAt : '';
  return sessionState;
}

export function agentActivityStatus(state, agentId, displayedSessionKey = '') {
  const sessions = Object.values(state?.sessions ?? {}).filter(
    (sessionState) => sessionState.agentId === agentId,
  );
  if (
    sessions.some(
      (sessionState) =>
        isRunActive(sessionState) &&
        runContributesToAgentActivity(sessionState),
    )
  ) {
    return AGENT_ACTIVITY_RUNNING;
  }
  if (
    sessions.some(
      (sessionState) =>
        sessionState.key !== displayedSessionKey &&
        sessionState.hasUnreadCompletion,
    )
  ) {
    return AGENT_ACTIVITY_UNREAD;
  }
  return AGENT_ACTIVITY_IDLE;
}

export function newestUnreadSessionForAgent(state, agentId) {
  const unreadSessions = Object.values(state?.sessions ?? {}).filter(
    (sessionState) =>
      sessionState.agentId === agentId &&
      sessionState.hasUnreadCompletion &&
      sessionState.unreadRunId,
  );
  unreadSessions.sort(
    (left, right) =>
      activityTimestamp(right.unreadRunAt || right.lastActiveAt) -
        activityTimestamp(left.unreadRunAt || left.lastActiveAt) ||
      left.sessionId.localeCompare(right.sessionId),
  );
  return unreadSessions[0] ?? null;
}

export function sessionHasTerminalRun(sessionState, runId) {
  if (!sessionState || !runId) {
    return false;
  }
  return (
    (sessionState.messages ?? []).some(
      (message) => message?.role === 'run_summary' && message?.run_id === runId,
    ) ||
    (sessionState.runEvents ?? []).some(
      (event) =>
        event?.run_id === runId && TERMINAL_RUN_EVENTS.has(event?.type),
    )
  );
}

function clearSessionCompletionActivity(sessionState) {
  sessionState.hasUnreadCompletion = false;
  sessionState.latestCompletionRunId = '';
  sessionState.unreadRunId = '';
  sessionState.unreadRunStatus = '';
  sessionState.unreadRunAt = '';
}

function normalizedActivityText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function activityTimestamp(value) {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function isAtLeastAsNewActivityTimestamp(left, right) {
  return activityTimestamp(left) >= activityTimestamp(right);
}

export function currentSessionState(state) {
  const agent = selectedAgent(state);
  if (!agent?.current_session_id) {
    return null;
  }
  return state.sessions[sessionKey(agent.id, agent.current_session_id)] ?? null;
}

export function updateSessionUsage(sessionState, usage) {
  sessionState.usage = usage;
  return sessionState;
}

function normalizeServerQueuedItem(item) {
  return {
    id: item.id,
    content: typeof item?.content === 'string' ? item.content : '',
    editable: item?.editable === true,
    created_at: typeof item?.created_at === 'string' ? item.created_at : null,
  };
}

export function syncQueueFromServer(sessionState, serverItems) {
  const normalizedItems = Array.isArray(serverItems)
    ? serverItems
        .filter((item) => typeof item?.id === 'string' && item.id.length > 0)
        .map((item) => normalizeServerQueuedItem(item))
    : [];
  sessionState.queue = normalizedItems;
  return sessionState.queue;
}

export function addServerQueuedMessage(sessionState, item) {
  if (!item || typeof item.id !== 'string' || item.id.length === 0) {
    return null;
  }

  const normalizedItem = normalizeServerQueuedItem(item);
  const existingIndex = sessionState.queue.findIndex(
    (queuedItem) => queuedItem.id === normalizedItem.id,
  );
  if (existingIndex >= 0) {
    sessionState.queue = sessionState.queue.map((queuedItem, index) =>
      index === existingIndex ? normalizedItem : queuedItem,
    );
    return normalizedItem;
  }

  sessionState.queue = [...sessionState.queue, normalizedItem];
  return normalizedItem;
}

export function updateQueuedMessageContent(
  sessionState,
  itemId,
  newContent,
  { editable } = {},
) {
  const queuedItem = sessionState.queue.find((item) => item.id === itemId);
  if (!queuedItem) {
    return false;
  }
  queuedItem.content = newContent;
  if (typeof editable === 'boolean') {
    queuedItem.editable = editable;
  }
  return true;
}

export function removeQueuedMessage(sessionState, queuedMessageId) {
  const originalLength = sessionState.queue.length;
  sessionState.queue = sessionState.queue.filter(
    (message) => message.id !== queuedMessageId,
  );
  return sessionState.queue.length !== originalLength;
}

export function isSessionEmpty(sessionState) {
  return Boolean(
    sessionState?.historyLoaded &&
    !sessionState.currentRun &&
    (sessionState.messages ?? []).length === 0 &&
    (sessionState.runEvents ?? []).length === 0 &&
    (sessionState.streamingRunEvents ?? []).length === 0 &&
    (sessionState.queue ?? []).length === 0,
  );
}

export function isRunActive(sessionState) {
  return sessionState?.status === CHAT_STATUS_RUNNING;
}

function runContributesToAgentActivity(sessionState) {
  return sessionState?.currentRun?.contributesToAgentActivity !== false;
}

// Reset a session's live Run state when freshly loaded History has confirmed
// the Run is no longer active (e.g. the terminal event was missed, the SSE
// stream gave up, the bus buffer rolled, or the server restarted). History is
// now the complete authoritative display source, so retained Run replay must
// be discarded with the active marker. Otherwise sparse replay containing only
// user_message_persisted events is appended behind History as duplicate User
// messages and empty Assistant runs.
export function resetStaleRun(sessionState) {
  if (!sessionState) {
    return sessionState;
  }
  sessionState.status = CHAT_STATUS_IDLE;
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  sessionState.streamError = '';
  sessionState.currentRun = null;
  sessionState.runEvents = [];
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
  return sessionState;
}
