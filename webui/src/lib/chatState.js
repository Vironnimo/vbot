export const CHAT_STATUS_IDLE = 'idle';
export const CHAT_STATUS_LOADING = 'loading';
export const CHAT_STATUS_RUNNING = 'running';
export const CHAT_STATUS_COMPLETED = 'completed';
export const CHAT_STATUS_FAILED = 'failed';
export const CHAT_STATUS_CANCELLED = 'cancelled';

export const TERMINAL_RUN_EVENTS = new Set([
  'run_completed',
  'run_failed',
  'run_cancelled',
]);

let queuedMessageCounter = 0;

export function createChatState() {
  return {
    agents: [],
    selectedAgentId: '',
    sessions: {},
    loadingAgents: false,
    agentsError: null,
  };
}

export function setAgents(state, agents) {
  state.agents = Array.isArray(agents) ? agents : [];
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
      runEvents: [],
      currentRun: null,
      queue: [],
      status: CHAT_STATUS_IDLE,
      error: null,
      streamStatus: CHAT_STATUS_IDLE,
    };
  }
  return state.sessions[key];
}

export function currentSessionState(state) {
  const agent = selectedAgent(state);
  if (!agent?.current_session_id) {
    return null;
  }
  return ensureSessionState(state, agent.id, agent.current_session_id);
}

export function loadHistory(sessionState, messages) {
  sessionState.messages = Array.isArray(messages) ? messages : [];
  sessionState.runEvents = [];
  sessionState.error = null;
  if (!isRunActive(sessionState)) {
    sessionState.status = CHAT_STATUS_IDLE;
  }
  return sessionState;
}

export function startRun(sessionState, run) {
  sessionState.currentRun = {
    runId: run.run_id,
    sseUrl: run.sse_url,
    status: run.status ?? CHAT_STATUS_RUNNING,
  };
  sessionState.status = CHAT_STATUS_RUNNING;
  sessionState.error = null;
  sessionState.streamStatus = CHAT_STATUS_RUNNING;
  appendRunEvents(sessionState, run.events ?? []);
  return sessionState.currentRun;
}

export function appendRunEvent(sessionState, event) {
  const normalizedEvent = normalizeRunEvent(event);
  if (!normalizedEvent) {
    return null;
  }
  if (
    sessionState.runEvents.some(
      (existingEvent) =>
        existingEvent.sequence === normalizedEvent.sequence &&
        existingEvent.run_id === normalizedEvent.run_id,
    )
  ) {
    return normalizedEvent;
  }

  sessionState.runEvents = [...sessionState.runEvents, normalizedEvent];
  if (TERMINAL_RUN_EVENTS.has(normalizedEvent.type)) {
    finishRun(sessionState, normalizedEvent);
  }
  return normalizedEvent;
}

export function appendRunEvents(sessionState, events) {
  for (const event of events) {
    appendRunEvent(sessionState, event);
  }
  return sessionState.runEvents;
}

export function finishRun(sessionState, event) {
  const type = event?.type;
  const status = event?.payload?.status;
  if (sessionState.currentRun) {
    sessionState.currentRun.status = status ?? terminalStatus(type);
  }
  sessionState.status = status ?? terminalStatus(type);
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  if (type === 'run_failed') {
    sessionState.error = event?.payload?.error ?? 'Run failed';
  }
  return sessionState;
}

export function markSessionError(sessionState, error) {
  sessionState.status = CHAT_STATUS_FAILED;
  sessionState.error = error?.message ?? String(error);
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  return sessionState;
}

export function enqueueMessage(sessionState, content) {
  const trimmedContent = content.trim();
  if (!trimmedContent) {
    return null;
  }
  const queuedMessage = {
    id: `queued-${queuedMessageCounter}`,
    content: trimmedContent,
  };
  queuedMessageCounter += 1;
  sessionState.queue = [...sessionState.queue, queuedMessage];
  return queuedMessage;
}

export function dequeueMessage(sessionState) {
  const [nextMessage, ...remainingMessages] = sessionState.queue;
  sessionState.queue = remainingMessages;
  return nextMessage ?? null;
}

export function removeQueuedMessage(sessionState, queuedMessageId) {
  const originalLength = sessionState.queue.length;
  sessionState.queue = sessionState.queue.filter(
    (message) => message.id !== queuedMessageId,
  );
  return sessionState.queue.length !== originalLength;
}

export function canCreateNewSession(sessionState) {
  return !sessionState || !isRunActive(sessionState);
}

export function isRunActive(sessionState) {
  return sessionState?.status === CHAT_STATUS_RUNNING;
}

export function visibleTimelineItems(sessionState) {
  if (!sessionState) {
    return [];
  }
  return [
    ...sessionState.messages.map((message) => ({
      id: message.id ?? `history-${message.role}-${message.timestamp}`,
      type: 'message',
      message,
    })),
    ...sessionState.runEvents
      .filter((event) => event.type !== 'run_started')
      .map((event) => ({
        id: `event-${event.run_id ?? 'run'}-${event.sequence ?? event.timestamp ?? event.type}`,
        type: 'event',
        event,
      })),
  ];
}

export function normalizeRunEvent(event) {
  if (!event || typeof event !== 'object') {
    return null;
  }
  if (event.data && typeof event.data === 'object') {
    return normalizeRunEvent(event.data);
  }
  if (!event.type) {
    return null;
  }
  return {
    sequence: event.sequence,
    run_id: event.run_id,
    agent_id: event.agent_id,
    session_id: event.session_id,
    type: event.type,
    payload: event.payload ?? {},
    timestamp: event.timestamp,
  };
}

function terminalStatus(eventType) {
  if (eventType === 'run_failed') {
    return CHAT_STATUS_FAILED;
  }
  if (eventType === 'run_cancelled') {
    return CHAT_STATUS_CANCELLED;
  }
  return CHAT_STATUS_COMPLETED;
}
