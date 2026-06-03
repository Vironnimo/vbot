import {
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
  RUN_EVENT_TOOL_CALL_STDERR,
  RUN_EVENT_TOOL_CALL_STDOUT,
} from './api.js';

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
      streamingItems: [],
      streamingRunEvents: [],
      streamingPhase: 0,
      seenStreamingEventKeys: new Set(),
      currentRun: null,
      queue: [],
      status: CHAT_STATUS_IDLE,
      error: null,
      streamStatus: CHAT_STATUS_IDLE,
      usage: null,
      hasOlderHistory: false,
      loadingOlderHistory: false,
    };
  }
  return state.sessions[key];
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

export function loadHistory(sessionState, messages, options = {}) {
  const activeRunEvents = isRunActive(sessionState)
    ? sessionState.runEvents
    : [];
  const activeStreamingItems = isRunActive(sessionState)
    ? sessionState.streamingItems
    : [];
  const activeStreamingRunEvents = isRunActive(sessionState)
    ? sessionState.streamingRunEvents
    : [];
  const activeStreamingPhase = isRunActive(sessionState)
    ? sessionState.streamingPhase
    : 0;
  const activeSeenStreamingEventKeys = isRunActive(sessionState)
    ? sessionState.seenStreamingEventKeys
    : new Set();
  sessionState.messages = Array.isArray(messages)
    ? messages.filter(isVisibleHistoryMessage)
    : [];
  sessionState.hasOlderHistory = options.hasMore === true;
  sessionState.runEvents = activeRunEvents;
  sessionState.streamingItems = activeStreamingItems;
  sessionState.streamingRunEvents = activeStreamingRunEvents;
  sessionState.streamingPhase = activeStreamingPhase;
  sessionState.seenStreamingEventKeys = activeSeenStreamingEventKeys;
  sessionState.error = null;
  if (!isRunActive(sessionState)) {
    sessionState.status = CHAT_STATUS_IDLE;
  }
  const lastUsage = findLastUsage(sessionState.messages);
  if (lastUsage) {
    sessionState.usage = lastUsage;
  }
  return sessionState;
}

export function prependHistory(sessionState, messages, options = {}) {
  const existingIds = new Set(
    (sessionState.messages ?? [])
      .map((message) => message?.id)
      .filter((id) => typeof id === 'string' && id.length > 0),
  );
  const olderMessages = Array.isArray(messages)
    ? messages
        .filter(isVisibleHistoryMessage)
        .filter((message) => !message?.id || !existingIds.has(message.id))
    : [];

  sessionState.messages = [...olderMessages, ...(sessionState.messages ?? [])];
  sessionState.hasOlderHistory = options.hasMore === true;
  return sessionState;
}

function findLastUsage(messages) {
  for (let i = (messages ?? []).length - 1; i >= 0; i--) {
    if (messages[i]?.role === 'assistant' && messages[i]?.usage) {
      return messages[i].usage;
    }
  }
  return null;
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
  sessionState.streamingItems = [];
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
  appendRunEvents(sessionState, run.events ?? []);
  return sessionState.currentRun;
}

export function appendRunEvent(sessionState, event) {
  const normalizedEvent = normalizeRunEvent(event);
  if (!normalizedEvent) {
    return null;
  }
  if (isStreamingDeltaRunEvent(normalizedEvent.type)) {
    const eventKey = streamingDeltaEventKey(normalizedEvent);
    if (eventKey && sessionState.seenStreamingEventKeys.has(eventKey)) {
      return normalizedEvent;
    }
    if (eventKey) {
      sessionState.seenStreamingEventKeys.add(eventKey);
    }
    updateStreamingItems(sessionState, normalizedEvent);
    appendCompressedStreamingRunEvent(sessionState, normalizedEvent);
    return normalizedEvent;
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
  if (normalizedEvent.type === 'run_started') {
    beginRunFromEvent(sessionState, normalizedEvent);
  }
  updateStreamingItems(sessionState, normalizedEvent);
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

function beginRunFromEvent(sessionState, event) {
  const currentRun = sessionState.currentRun;
  const isSameRun = currentRun?.runId === event.run_id;
  const currentSseUrl = isSameRun ? currentRun.sseUrl : '';
  sessionState.currentRun = {
    runId: event.run_id,
    sseUrl: currentSseUrl,
    status: CHAT_STATUS_RUNNING,
  };
  sessionState.status = CHAT_STATUS_RUNNING;
  sessionState.error = null;
  sessionState.streamStatus = CHAT_STATUS_RUNNING;
  if (isSameRun) {
    return;
  }
  sessionState.streamingItems = [];
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
}

export function appendCompactionCheckpoint(sessionState, message) {
  if (!message || message.role !== 'compaction_checkpoint') {
    return;
  }

  if (
    message.id &&
    sessionState.messages.some((existing) => existing?.id === message.id)
  ) {
    return;
  }

  sessionState.messages = [...sessionState.messages, message];
}

export function finishRun(sessionState, event) {
  const type = event?.type;
  const status = event?.payload?.status;
  if (sessionState.currentRun) {
    sessionState.currentRun.status = status ?? terminalStatus(type);
  }
  sessionState.status = status ?? terminalStatus(type);
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  sessionState.streamingItems = [];
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
  if (type === 'run_failed') {
    sessionState.error = event?.payload?.error ?? 'Run failed';
  }
  if (type === 'run_completed' && event?.payload?.usage) {
    updateSessionUsage(sessionState, event.payload.usage);
  }
  return sessionState;
}

export function highestRunEventSequence(sessionState) {
  const streamingItemSequences = (sessionState?.streamingItems ?? [])
    .map((item) => item.sequence)
    .filter((sequence) => Number.isFinite(sequence));
  return Math.max(
    0,
    ...(sessionState?.runEvents ?? [])
      .map((event) => event.sequence)
      .filter((sequence) => Number.isFinite(sequence)),
    ...streamingItemSequences,
  );
}

export function highestContiguousRunEventSequence(sessionState) {
  const runId = activeRunIdForReplay(sessionState);
  if (!runId) {
    return 0;
  }

  const sequences = new Set();
  for (const event of sessionState?.runEvents ?? []) {
    addSequenceForRun(sequences, event, runId);
  }
  for (const event of sessionState?.streamingRunEvents ?? []) {
    if (event?.run_id !== runId) {
      continue;
    }
    addCompressedStreamingEventSequences(sequences, event);
  }
  for (const eventKey of sessionState?.seenStreamingEventKeys ?? []) {
    addStreamingEventKeySequenceForRun(sequences, eventKey, runId);
  }

  return highestContiguousSequence(sequences);
}

function activeRunIdForReplay(sessionState) {
  if (sessionState?.currentRun?.runId) {
    return sessionState.currentRun.runId;
  }
  return latestRunIdFromEvents([
    ...(sessionState?.runEvents ?? []),
    ...(sessionState?.streamingRunEvents ?? []),
  ]);
}

function latestRunIdFromEvents(events) {
  for (let index = (events ?? []).length - 1; index >= 0; index -= 1) {
    const runId = events[index]?.run_id;
    if (typeof runId === 'string' && runId.length > 0) {
      return runId;
    }
  }
  return '';
}

function addSequenceForRun(sequences, event, runId) {
  if (event?.run_id !== runId) {
    return;
  }
  addSequence(sequences, event.sequence);
}

function addCompressedStreamingEventSequences(sequences, event) {
  const firstSequence = event?.sequence;
  const latestSequence = streamEventLatestSequence(event);
  const chunkCount = streamEventChunkCount(event);
  if (
    Number.isFinite(firstSequence) &&
    Number.isFinite(latestSequence) &&
    latestSequence >= firstSequence &&
    latestSequence - firstSequence + 1 === chunkCount
  ) {
    for (
      let sequence = firstSequence;
      sequence <= latestSequence;
      sequence += 1
    ) {
      addSequence(sequences, sequence);
    }
    return;
  }
  addSequence(sequences, firstSequence);
  addSequence(sequences, latestSequence);
}

function addStreamingEventKeySequenceForRun(sequences, eventKey, runId) {
  if (typeof eventKey !== 'string') {
    return;
  }
  const parts = eventKey.split(':');
  if (parts.length < 3) {
    return;
  }
  const sequence = Number(parts.at(-1));
  const eventRunId = parts.slice(0, -2).join(':');
  if (eventRunId !== runId) {
    return;
  }
  addSequence(sequences, sequence);
}

function addSequence(sequences, sequence) {
  if (!Number.isFinite(sequence) || sequence < 1) {
    return;
  }
  sequences.add(Math.trunc(sequence));
}

function highestContiguousSequence(sequences) {
  let sequence = 0;
  while (sequences.has(sequence + 1)) {
    sequence += 1;
  }
  return sequence;
}

export function markSessionError(sessionState, error) {
  if (sessionState.currentRun) {
    sessionState.currentRun.status = CHAT_STATUS_FAILED;
  }
  sessionState.status = CHAT_STATUS_FAILED;
  sessionState.error = error?.message ?? String(error);
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  sessionState.streamingItems = [];
  sessionState.streamingRunEvents = [];
  return sessionState;
}

function normalizeServerQueuedItem(item) {
  return {
    id: item.id,
    content: typeof item?.content === 'string' ? item.content : '',
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

export function updateQueuedMessageContent(sessionState, itemId, newContent) {
  const queuedItem = sessionState.queue.find((item) => item.id === itemId);
  if (!queuedItem) {
    return false;
  }
  queuedItem.content = newContent;
  return true;
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
  return buildVisibleTimelineItems(sessionState, sessionState?.runEvents ?? []);
}

export function visibleTimelineItemsForRender(sessionState) {
  if (!sessionState) {
    return [];
  }

  return buildVisibleTimelineItems(
    sessionState,
    [
      ...(sessionState.runEvents ?? []),
      ...(sessionState.streamingRunEvents ?? []),
    ],
    {
      includeStreamingAssistantAndReasoning: false,
      includeStreamingToolCalls: false,
    },
  );
}

export function assistantRunChildProgressKey(child) {
  if (!child || typeof child !== 'object') {
    return '0::0';
  }

  const { chunkCount, latestSequence } = childStreamingProgress(child);
  if (child.type === 'tool_call') {
    const toolNameLength = (child.name ?? '').length;
    const streamedArgumentsLength = (child.partialArgumentsText ?? '').length;
    const finalizedArgumentsLength =
      typeof child.arguments === 'string' ? child.arguments.length : 0;
    const outputLength =
      (child.stdout ?? '').length + (child.stderr ?? '').length;
    return `${chunkCount}:${latestSequence ?? ''}:${toolNameLength}:${streamedArgumentsLength + finalizedArgumentsLength}:${outputLength}:${child.resultEvent ? 1 : 0}`;
  }

  const contentLength =
    typeof child.content === 'string' ? child.content.length : 0;
  return `${chunkCount}:${latestSequence ?? ''}:${contentLength}`;
}

function buildVisibleTimelineItems(sessionState, runEvents, options = {}) {
  if (!sessionState) {
    return [];
  }

  const {
    includeStreamingAssistantAndReasoning = true,
    includeStreamingToolCalls = true,
  } = options;

  const historyItems = historyTimelineItems(sessionState.messages);
  const liveItems = liveTimelineItems(runEvents);
  const reconciledItems = shouldSelectTrackedRunSource(sessionState, runEvents)
    ? selectTrackedRunTimelineSource(
        sessionState,
        historyItems,
        liveItems,
        runEvents,
      )
    : [...historyItems, ...liveItems];

  const strippedReconciledItems = reconciledItems.map((item) =>
    stripTimelineSequence(item),
  );

  const streamingTimelineItems = (sessionState.streamingItems ?? [])
    .filter((item) => {
      if (item.type === 'tool_call') {
        return (
          includeStreamingToolCalls &&
          !streamingToolCallAlreadyRendered(strippedReconciledItems, item)
        );
      }
      if (item.type === 'assistant' || item.type === 'reasoning') {
        return includeStreamingAssistantAndReasoning;
      }
      return true;
    })
    .map((item) => ({
      id: item.id,
      type: 'streaming',
      streamingItem: item,
    }));

  return [...strippedReconciledItems, ...streamingTimelineItems];
}

function streamingToolCallAlreadyRendered(reconciledItems, streamingItem) {
  if (streamingItem?.type !== 'tool_call' || !streamingItem.toolCallId) {
    return false;
  }

  return (reconciledItems ?? []).some((item) => {
    if (item?.type !== 'assistant_run') {
      return false;
    }

    if (!runIdsMatch(item.runId ?? item.run_id, streamingItem.run_id)) {
      return false;
    }

    return (item.tools ?? item.items ?? []).some(
      (child) =>
        child?.type === 'tool_call' &&
        child.toolCallId === streamingItem.toolCallId,
    );
  });
}

function runIdsMatch(leftRunId, rightRunId) {
  return Boolean(leftRunId) && Boolean(rightRunId) && leftRunId === rightRunId;
}

function childStreamingProgress(child) {
  let chunkCount = 0;
  let latestSequence = Number.isFinite(child?.sequence) ? child.sequence : null;

  for (const event of child?.events ?? []) {
    chunkCount += streamEventChunkCount(event);
    const eventLatestSequence = streamEventLatestSequence(event);
    if (!Number.isFinite(eventLatestSequence)) {
      continue;
    }
    latestSequence = Number.isFinite(latestSequence)
      ? Math.max(latestSequence, eventLatestSequence)
      : eventLatestSequence;
  }

  if (chunkCount === 0) {
    chunkCount = (child?.messages ?? []).length;
  }

  return { chunkCount, latestSequence };
}

function streamEventChunkCount(event) {
  if (
    Number.isFinite(event?._streamChunkCount) &&
    event._streamChunkCount > 0
  ) {
    return event._streamChunkCount;
  }
  return 1;
}

function streamEventLatestSequence(event) {
  if (Number.isFinite(event?._streamLatestSequence)) {
    return event._streamLatestSequence;
  }
  return event?.sequence;
}

function shouldSelectTrackedRunSource(
  sessionState,
  runEvents = sessionState?.runEvents,
) {
  return (
    Boolean(sessionState?.currentRun?.runId) &&
    Array.isArray(runEvents) &&
    runEvents.length > 0
  );
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

function historyTimelineItems(messages) {
  const timelineItems = [];
  let activeAssistantRun = null;
  let previousVisibleRole = '';

  for (const message of messages ?? []) {
    if (message?.role === 'compaction_checkpoint') {
      pushActiveAssistantRun(timelineItems, activeAssistantRun);
      activeAssistantRun = null;
      timelineItems.push({
        id: `compaction-${message.id ?? message.timestamp}`,
        type: 'compaction_separator',
        timestamp: message.timestamp,
        message,
      });
      previousVisibleRole = 'compaction_checkpoint';
      continue;
    }

    if (message?.role === 'run_summary') {
      if (activeAssistantRun) {
        appendHistoryRunSummary(activeAssistantRun, message);
        pushActiveAssistantRun(timelineItems, activeAssistantRun);
        activeAssistantRun = null;
      }
      previousVisibleRole = 'run_summary';
      continue;
    }

    if (message?.role === 'user') {
      pushActiveAssistantRun(timelineItems, activeAssistantRun);
      activeAssistantRun = null;
      timelineItems.push(historyMessageItem(message));
      previousVisibleRole = 'user';
      continue;
    }

    if (message?.role === 'assistant') {
      const followsAssistant = previousVisibleRole === 'assistant';
      if (followsAssistant) {
        pushActiveAssistantRun(timelineItems, activeAssistantRun);
        activeAssistantRun = null;
      }

      if (
        !activeAssistantRun &&
        (hasToolCalls(message) ||
          previousTimelineItemIsUser(timelineItems) ||
          followsAssistant)
      ) {
        activeAssistantRun = createAssistantRunItem({
          id: `history-run-${message.id ?? message.timestamp ?? timelineItems.length}`,
          runId: null,
          source: 'history',
          sequence: timelineItems.length,
          timestamp: message.timestamp,
        });
      }

      if (activeAssistantRun) {
        appendHistoryAssistantMessage(activeAssistantRun, message);
        previousVisibleRole = 'assistant';
        continue;
      }

      timelineItems.push(historyMessageItem(message));
      previousVisibleRole = 'assistant';
      continue;
    }

    if (message?.role === 'tool' && activeAssistantRun) {
      appendHistoryToolResult(activeAssistantRun, message);
      previousVisibleRole = 'tool';
      continue;
    }

    pushActiveAssistantRun(timelineItems, activeAssistantRun);
    activeAssistantRun = null;
    timelineItems.push(historyMessageItem(message));
    previousVisibleRole = message?.role ?? '';
  }

  pushActiveAssistantRun(timelineItems, activeAssistantRun);
  return timelineItems;
}

function isVisibleHistoryMessage(message) {
  return [
    'user',
    'assistant',
    'tool',
    'error',
    'compaction_checkpoint',
    'run_summary',
  ].includes(message?.role);
}

function selectTrackedRunTimelineSource(
  sessionState,
  historyItems,
  liveItems,
  runEvents = sessionState?.runEvents,
) {
  const activeRunId = sessionState.currentRun?.runId ?? null;
  const liveAssistantRun = liveItems.find(
    (item) =>
      item.type === 'assistant_run' && matchesRunId(item.runId, activeRunId),
  );
  if (!liveAssistantRun) {
    return [...historyItems, ...liveItems];
  }

  const activeUserEvent = activeRunUserEvent(runEvents, activeRunId);
  if (!activeUserEvent?.payload?.message) {
    return [...historyItems, ...liveItems];
  }

  const currentUserIndex = findMatchingHistoryUserIndex(
    sessionState.messages,
    activeUserEvent.payload.message,
  );
  if (currentUserIndex < 0) {
    return [...historyItems, ...liveItems];
  }

  const { prefixMessages, currentTurnMessages, trailingMessages } =
    splitHistoryAroundActiveUser(sessionState.messages, currentUserIndex);
  const remainingLiveItems = liveItems.filter(
    (item) => !matchesActiveRunTimelineItem(item, activeRunId),
  );

  if (
    isTrackedRunTerminal(sessionState, liveAssistantRun) &&
    hasPersistedAssistantTurn(currentTurnMessages)
  ) {
    return [...historyItems, ...remainingLiveItems];
  }

  const activeUserItem = historyMessageItem(
    sessionState.messages[currentUserIndex],
  );
  const prefixHistoryItems = historyTimelineItems(prefixMessages);
  const trailingHistoryItems = historyTimelineItems(trailingMessages);

  return [
    ...prefixHistoryItems,
    activeUserItem,
    liveAssistantRun,
    ...trailingHistoryItems,
    ...remainingLiveItems,
  ];
}

function splitHistoryAroundActiveUser(messages, activeUserIndex) {
  const prefixMessages = (messages ?? []).slice(0, activeUserIndex);
  const currentTurnMessages = [];
  const trailingMessages = [];
  let foundTrailingBoundary = false;

  for (const message of (messages ?? []).slice(activeUserIndex)) {
    if (
      currentTurnMessages.length > 0 &&
      !foundTrailingBoundary &&
      message?.role === 'user'
    ) {
      foundTrailingBoundary = true;
    }

    if (foundTrailingBoundary) {
      trailingMessages.push(message);
      continue;
    }

    currentTurnMessages.push(message);
  }

  return {
    prefixMessages,
    currentTurnMessages,
    trailingMessages,
  };
}

function isTrackedRunTerminal(sessionState, liveAssistantRun) {
  return (
    !isRunActive(sessionState) ||
    TERMINAL_RUN_EVENTS.has(liveAssistantRun.terminalEvent?.type) ||
    [CHAT_STATUS_COMPLETED, CHAT_STATUS_FAILED, CHAT_STATUS_CANCELLED].includes(
      sessionState.currentRun?.status,
    )
  );
}

function hasPersistedAssistantTurn(messages) {
  return (messages ?? []).some((message) =>
    ['assistant', 'tool'].includes(message?.role),
  );
}

function matchesActiveRunTimelineItem(item, activeRunId) {
  if (item?.type === 'assistant_run') {
    return matchesRunId(item.runId, activeRunId);
  }

  if (item?.type === 'event') {
    return matchesRunId(item.event?.run_id, activeRunId);
  }

  return false;
}

function activeRunUserEvent(runEvents, activeRunId) {
  return [...(runEvents ?? [])]
    .reverse()
    .find(
      (event) =>
        event?.type === 'user_message_persisted' &&
        matchesRunId(event.run_id, activeRunId),
    );
}

function findMatchingHistoryUserIndex(messages, userMessage) {
  const messageId = userMessage?.id;
  if (messageId) {
    const matchedById = (messages ?? []).findLastIndex(
      (message) => message?.role === 'user' && message.id === messageId,
    );
    if (matchedById >= 0) {
      return matchedById;
    }
  }

  const messageContent = userMessage?.content;
  if (!messageContent) {
    return -1;
  }

  return (messages ?? []).findLastIndex(
    (message) =>
      message?.role === 'user' &&
      message.content === messageContent &&
      (!userMessage.timestamp || message.timestamp === userMessage.timestamp),
  );
}

function matchesRunId(candidateRunId, activeRunId) {
  if (!activeRunId) {
    return true;
  }
  return candidateRunId === activeRunId;
}

function liveTimelineItems(runEvents) {
  const runGroups = new Map();
  const timelineEntries = [];

  for (const [arrivalIndex, event] of (runEvents ?? []).entries()) {
    if (isAssistantRunEvent(event)) {
      const runGroup = ensureLiveRunGroup(
        runGroups,
        timelineEntries,
        event,
        arrivalIndex,
      );
      runGroup.events.push(event);
      continue;
    }

    if (event?.type === 'error_message_persisted') {
      const message = event.payload?.message;
      if (message) {
        timelineEntries.push({
          kind: 'standalone',
          order: arrivalIndex,
          item: historyMessageItem(message),
        });
      }
      continue;
    }

    if (shouldShowStandaloneRunEvent(event)) {
      const eventItem = createStandaloneRunEventItem(event);
      if (event.run_id) {
        const runGroup = ensureLiveRunGroup(
          runGroups,
          timelineEntries,
          event,
          arrivalIndex,
        );
        runGroup.userItem = eventItem;
        continue;
      }

      timelineEntries.push({
        kind: 'standalone',
        order: arrivalIndex,
        item: eventItem,
      });
    }
  }

  return timelineEntries
    .sort((left, right) => left.order - right.order)
    .flatMap((entry) => liveTimelineEntryItems(entry));
}

function ensureLiveRunGroup(runGroups, timelineEntries, event, arrivalIndex) {
  const runKey = event.run_id ?? 'run';
  if (runGroups.has(runKey)) {
    return runGroups.get(runKey);
  }

  const runGroup = {
    kind: 'run',
    order: arrivalIndex,
    runKey,
    events: [],
    userItem: null,
  };
  runGroups.set(runKey, runGroup);
  timelineEntries.push(runGroup);
  return runGroup;
}

function liveTimelineEntryItems(entry) {
  if (entry.kind === 'standalone') {
    return [entry.item];
  }

  const assistantRun = buildLiveAssistantRunItem(entry.runKey, entry.events);
  return [entry.userItem, assistantRun].filter(Boolean);
}

function createStandaloneRunEventItem(event) {
  return {
    id: `event-${event.run_id ?? 'run'}-${event.sequence ?? event.timestamp ?? event.type}`,
    type: 'event',
    event,
  };
}

function buildLiveAssistantRunItem(runKey, events) {
  const orderedEvents = [...events].sort(compareRunEvents);
  const firstEvent = orderedEvents[0] ?? {};
  const runId = firstEvent.run_id ?? runKey;
  const assistantRun = createAssistantRunItem({
    id: `assistant-run-${runKey}`,
    runId,
    source: 'live',
    sequence: firstEvent.sequence ?? 0,
    timestamp: firstEvent.timestamp,
  });
  assistantRun.events = orderedEvents;

  for (const event of orderedEvents) {
    appendLiveRunEvent(assistantRun, event);
  }

  syncAssistantRunCollections(assistantRun);
  return assistantRun;
}

function createAssistantRunItem({ id, runId, source, sequence, timestamp }) {
  return {
    id,
    type: 'assistant_run',
    source,
    runId,
    run_id: runId,
    sequence,
    timestamp,
    startTimestamp: timestamp,
    endTimestamp: null,
    status: CHAT_STATUS_RUNNING,
    timing: null,
    durationMs: null,
    items: [],
    reasoning: [],
    outputs: [],
    tools: [],
    events: [],
  };
}

function appendLiveRunEvent(assistantRun, event) {
  if (event.type === 'run_started') {
    assistantRun.startTimestamp =
      event.timestamp ?? assistantRun.startTimestamp;
    assistantRun.status = event.payload?.status ?? CHAT_STATUS_RUNNING;
    return;
  }

  if (event.type === 'model_fallback_activated') {
    const toModel = event.payload?.to_model ?? '';
    const fromModel = event.payload?.from_model ?? '';
    assistantRun.items.push({
      id: `model-fallback-${assistantRun.id}-${event.sequence ?? assistantRun.items.length}`,
      type: 'model_fallback',
      content: toModel,
      from_model: fromModel,
      to_model: toModel,
      sequence: event.sequence ?? assistantRun.items.length,
      timestamp: event.timestamp,
      events: [event],
    });
    syncAssistantRunCollections(assistantRun);
    return;
  }

  if (TERMINAL_RUN_EVENTS.has(event.type)) {
    const timing = normalizedTiming(event.payload?.timing);
    assistantRun.endTimestamp = event.timestamp ?? assistantRun.endTimestamp;
    assistantRun.startTimestamp =
      timing?.started_at ?? assistantRun.startTimestamp;
    assistantRun.endTimestamp =
      timing?.completed_at ?? assistantRun.endTimestamp;
    assistantRun.timing = timing ?? assistantRun.timing;
    assistantRun.durationMs =
      timingDurationMs(timing) ?? assistantRun.durationMs;
    assistantRun.status = event.payload?.status ?? terminalStatus(event.type);
    assistantRun.terminalEvent = event;
    if (event.type === 'run_cancelled') {
      markPendingToolsCancelled(assistantRun, event);
    }
    return;
  }

  if (event.type === RUN_EVENT_REASONING_DELTA) {
    appendTextSection(assistantRun, {
      type: 'reasoning',
      content: event.payload?.reasoning_delta,
      event,
      streaming: true,
    });
    return;
  }

  if (event.type === 'reasoning') {
    appendTextSection(assistantRun, {
      type: 'reasoning',
      content: textFromRunEventMessage(event, 'reasoning'),
      event,
      streaming: false,
    });
    return;
  }

  if (event.type === RUN_EVENT_ASSISTANT_OUTPUT_DELTA) {
    appendTextSection(assistantRun, {
      type: 'assistant_output',
      content: event.payload?.content_delta,
      event,
      streaming: true,
    });
    return;
  }

  if (event.type === 'assistant_output') {
    const message = event.payload?.message;
    if (message?.reasoning) {
      appendTextSection(assistantRun, {
        type: 'reasoning',
        content: message.reasoning,
        event,
        streaming: false,
      });
    }

    appendTextSection(assistantRun, {
      type: 'assistant_output',
      content: textFromRunEventMessage(event, 'content'),
      event,
      streaming: false,
    });
    return;
  }

  if (event.type === RUN_EVENT_TOOL_CALL_DELTA) {
    appendToolDelta(assistantRun, event);
    return;
  }

  if (event.type === 'tool_call_started') {
    mergeToolStarted(assistantRun, event);
    return;
  }

  if (
    event.type === RUN_EVENT_TOOL_CALL_STDOUT ||
    event.type === RUN_EVENT_TOOL_CALL_STDERR
  ) {
    mergeToolOutput(assistantRun, event);
    return;
  }

  if (event.type === 'tool_call_result') {
    mergeToolResult(assistantRun, event);
    return;
  }

  if (event.type === 'subagent_session_started') {
    mergeSubAgentSessionStarted(assistantRun, event);
  }
}

function appendHistoryAssistantMessage(assistantRun, message) {
  if (message.reasoning) {
    appendTextSection(assistantRun, {
      type: 'reasoning',
      content: message.reasoning,
      message,
      streaming: false,
    });
  }

  if (message.content) {
    appendTextSection(assistantRun, {
      type: 'assistant_output',
      content: message.content,
      message,
      streaming: false,
    });
  }

  for (const [index, toolCall] of (message.tool_calls ?? []).entries()) {
    mergeToolStarted(assistantRun, {
      type: 'tool_call_started',
      sequence: assistantRun.items.length,
      timestamp: message.timestamp,
      payload: {
        tool_call: {
          index,
          ...toolCall,
        },
      },
    });
  }

  assistantRun.status = CHAT_STATUS_COMPLETED;
}

function appendHistoryToolResult(assistantRun, message) {
  mergeToolResult(assistantRun, {
    type: 'tool_call_result',
    sequence: assistantRun.items.length,
    timestamp: message.timestamp,
    payload: {
      tool_call: {
        id: message.tool_call_id,
        name: message.name,
      },
      result: message.content,
      message,
      timing: message.timing,
    },
  });
  assistantRun.status = hasResultFailure(message.content)
    ? CHAT_STATUS_FAILED
    : CHAT_STATUS_COMPLETED;
}

function appendHistoryRunSummary(assistantRun, message) {
  const timing = normalizedTiming(message?.timing);
  assistantRun.runId = message.run_id ?? assistantRun.runId;
  assistantRun.run_id = assistantRun.runId;
  assistantRun.status = message.status ?? assistantRun.status;
  assistantRun.timing = timing ?? assistantRun.timing;
  assistantRun.startTimestamp =
    timing?.started_at ?? assistantRun.startTimestamp;
  assistantRun.endTimestamp = timing?.completed_at ?? assistantRun.endTimestamp;
  assistantRun.durationMs = timingDurationMs(timing) ?? assistantRun.durationMs;
  assistantRun.runSummaryMessage = message;
}

function appendTextSection(
  assistantRun,
  { type, content, event = null, message = null, streaming },
) {
  if (!content) {
    return;
  }

  const sequence = event?.sequence ?? assistantRun.items.length;
  const existingItem = mergeableTextSection(assistantRun, {
    type,
    content,
    message: message ?? event?.payload?.message,
    streaming,
  });
  if (existingItem) {
    existingItem.content = streaming
      ? `${existingItem.content}${content}`
      : content;
    existingItem.sequence = firstSeenSequence(existingItem.sequence, sequence);
    existingItem.timestamp ??= event?.timestamp ?? message?.timestamp;
    existingItem.streaming = streaming;
    existingItem.events = [...(existingItem.events ?? []), event].filter(
      Boolean,
    );
    existingItem.messages = [...(existingItem.messages ?? []), message].filter(
      Boolean,
    );
    syncAssistantRunCollections(assistantRun);
    return;
  }

  assistantRun.items.push({
    id: `${type}-${assistantRun.id}-${sequence}`,
    type,
    content,
    sequence,
    timestamp: event?.timestamp ?? message?.timestamp,
    streaming,
    events: event ? [event] : [],
    messages: message ? [message] : [],
  });
  syncAssistantRunCollections(assistantRun);
}

function mergeableTextSection(
  assistantRun,
  { type, content, message = null, streaming },
) {
  const lastMatchingIndex = assistantRun.items.findLastIndex(
    (item) => item.type === type,
  );
  if (lastMatchingIndex < 0) {
    return null;
  }

  const lastMatchingItem = assistantRun.items[lastMatchingIndex];
  const interveningItems = assistantRun.items.slice(lastMatchingIndex + 1);
  if (interveningItems.length === 0) {
    return lastMatchingItem;
  }

  const onlyPendingToolRows = interveningItems.every(
    (item) => item.type === 'tool_call' && !item.resultEvent,
  );
  if (onlyPendingToolRows) {
    return lastMatchingItem;
  }

  if (!streaming) {
    return mergeableDraftAcrossFinalizedRows(
      assistantRun,
      type,
      content,
      message,
    );
  }

  return null;
}

function mergeableDraftAcrossFinalizedRows(
  assistantRun,
  type,
  content,
  message,
) {
  const draftIndex = assistantRun.items.findLastIndex(
    (item) => item.type === type && isFinalizableTextDraft(item),
  );
  if (draftIndex < 0) {
    return null;
  }

  const draftItem = assistantRun.items[draftIndex];
  const interveningItems = assistantRun.items.slice(draftIndex + 1);
  const hasFinalSameTypeAfterDraft = interveningItems.some(
    (item) => item.type === type && !isFinalizableTextDraft(item),
  );
  if (hasFinalSameTypeAfterDraft) {
    return null;
  }

  const hasClosedTextPhaseAfterDraft = interveningItems.some(
    (item) => item.type !== 'tool_call' && !isFinalizableTextDraft(item),
  );
  if (hasClosedTextPhaseAfterDraft) {
    return null;
  }

  const sharesCurrentToolPhase = messageSharesToolCallRows(
    message,
    interveningItems,
  );
  if (draftItem.content !== content && !sharesCurrentToolPhase) {
    return null;
  }

  const hasCompletedToolRowAfterDraft = interveningItems.some(
    (item) => item.type === 'tool_call' && item.resultEvent,
  );
  if (hasCompletedToolRowAfterDraft && !sharesCurrentToolPhase) {
    return null;
  }

  return draftItem;
}

function isFinalizableTextDraft(item) {
  const events = item.events ?? [];
  return (
    item.streaming ||
    (item.type === 'reasoning' &&
      events.some((event) => event?.type === 'reasoning') &&
      !events.some((event) => event?.type === 'assistant_output'))
  );
}

function messageSharesToolCallRows(message, toolRows) {
  const messageToolKeys = new Set(
    (message?.tool_calls ?? []).map((toolCall, index) =>
      toolKeyFromToolCall({ index, ...toolCall }),
    ),
  );
  if (messageToolKeys.size === 0) {
    return false;
  }

  return toolRows.some(
    (item) =>
      item.type === 'tool_call' &&
      (messageToolKeys.has(item.key) ||
        messageToolKeys.has(toolKeyFromValues(item.toolCallId, item.index))),
  );
}

function appendToolDelta(assistantRun, event) {
  const payload = event.payload ?? {};
  const toolKey = toolKeyFromValues(payload.tool_call_id ?? payload.id);
  const tool = upsertToolRow(assistantRun, toolKey, event, {
    id: payload.tool_call_id ?? payload.id,
  });
  tool.streaming = true;
  tool.toolCallId = payload.tool_call_id ?? payload.id ?? tool.toolCallId;
  tool.name = `${tool.name ?? ''}${payload.name_delta ?? ''}`;
  tool.partialArgumentsText = `${tool.partialArgumentsText ?? ''}${payload.arguments_delta ?? ''}`;
  tool.status = 'preparing';
  tool.events = [...tool.events, event];
  syncAssistantRunCollections(assistantRun);
}

function mergeToolStarted(assistantRun, event) {
  const toolCall = event.payload?.tool_call ?? {};
  const tool = upsertToolRow(
    assistantRun,
    toolKeyFromToolCall(toolCall),
    event,
    toolCall,
  );
  tool.streaming = false;
  tool.toolCall = toolCall;
  tool.toolCallId = toolCall.id ?? tool.toolCallId;
  tool.index = toolCall.index ?? tool.index;
  tool.name = toolCall.name ?? tool.name;
  tool.arguments = toolCall.arguments;
  tool.display = event.payload?.display ?? toolCall.display ?? null;
  tool.partialArgumentsText = null;
  tool.startedEvent = event;
  tool.status = tool.resultEvent ? tool.status : CHAT_STATUS_RUNNING;
  tool.events = [...tool.events, event];
  syncAssistantRunCollections(assistantRun);
}

function mergeToolOutput(assistantRun, event) {
  const payload = event.payload ?? {};
  const toolCallId = payload.tool_call_id ?? payload.id;
  const tool = upsertToolRow(
    assistantRun,
    toolKeyFromValues(toolCallId),
    event,
    {
      id: toolCallId,
    },
  );
  const key = event.type === RUN_EVENT_TOOL_CALL_STDERR ? 'stderr' : 'stdout';
  tool.toolCallId = toolCallId ?? tool.toolCallId;
  tool[key] = `${tool[key] ?? ''}${payload.data ?? ''}`;
  tool.outputEvents = [...(tool.outputEvents ?? []), event];
  tool.events = [...tool.events, event];
  syncAssistantRunCollections(assistantRun);
}

function mergeToolResult(assistantRun, event) {
  const toolCall = event.payload?.tool_call ?? {};
  const tool = upsertToolRow(
    assistantRun,
    toolKeyFromToolCall(toolCall),
    event,
    toolCall,
  );
  tool.toolCall = {
    ...(tool.toolCall ?? {}),
    ...toolCall,
  };
  tool.toolCallId = toolCall.id ?? tool.toolCallId;
  tool.index = toolCall.index ?? tool.index;
  tool.name = toolCall.name ?? tool.name;
  tool.result = event.payload?.result ?? event.payload?.message?.content;
  tool.resultEvent = event;
  tool.timing =
    normalizedTiming(event.payload?.timing ?? event.payload?.message?.timing) ??
    tool.timing;
  tool.durationMs = timingDurationMs(tool.timing) ?? tool.durationMs;
  tool.status = hasToolResultFailure(event) ? CHAT_STATUS_FAILED : 'success';
  tool.events = [...tool.events, event];
  syncAssistantRunCollections(assistantRun);
}

function mergeSubAgentSessionStarted(assistantRun, event) {
  const toolCall = event.payload?.tool_call ?? {};
  const data = event.payload?.data ?? {};
  const tool = upsertToolRow(
    assistantRun,
    toolKeyFromToolCall(toolCall),
    event,
    toolCall,
  );
  tool.toolCall = {
    ...(tool.toolCall ?? {}),
    ...toolCall,
  };
  tool.toolCallId = toolCall.id ?? tool.toolCallId;
  tool.index = toolCall.index ?? tool.index;
  tool.name = toolCall.name ?? tool.name;
  tool.subAgentSession = {
    ...(tool.subAgentSession ?? {}),
    ...(isPlainObject(data) ? data : {}),
  };
  tool.status = tool.resultEvent ? tool.status : CHAT_STATUS_RUNNING;
  tool.events = [...tool.events, event];
  syncAssistantRunCollections(assistantRun);
}

function upsertToolRow(assistantRun, key, event, toolCall = {}) {
  const existingTool = assistantRun.items.find(
    (item) =>
      item.type === 'tool_call' &&
      (item.key === key || toolMatchesCall(item, toolCall)),
  );
  if (existingTool) {
    existingTool.key = moreStableToolKey(existingTool.key, key);
    return existingTool;
  }

  const sequence = event?.sequence ?? assistantRun.items.length;
  const tool = {
    id: `tool-${assistantRun.id}-${key}`,
    type: 'tool_call',
    key,
    sequence,
    timestamp: event?.timestamp,
    status: CHAT_STATUS_RUNNING,
    name: '',
    arguments: undefined,
    display: null,
    partialArgumentsText: null,
    result: undefined,
    toolCall: null,
    startedEvent: null,
    resultEvent: null,
    timing: null,
    durationMs: null,
    stdout: '',
    stderr: '',
    outputEvents: [],
    events: [],
  };
  assistantRun.items.push(tool);
  syncAssistantRunCollections(assistantRun);
  return tool;
}

function markPendingToolsCancelled(assistantRun, event) {
  let changed = false;
  for (const item of assistantRun.items) {
    if (
      item.type !== 'tool_call' ||
      item.resultEvent ||
      item.status === CHAT_STATUS_COMPLETED ||
      item.status === CHAT_STATUS_FAILED ||
      item.status === CHAT_STATUS_CANCELLED ||
      item.status === 'success'
    ) {
      continue;
    }

    item.status = CHAT_STATUS_CANCELLED;
    item.endTimestamp = event.timestamp ?? item.endTimestamp;
    item.cancelledEvent = event;
    item.events = [...(item.events ?? []), event];
    changed = true;
  }

  if (changed) {
    syncAssistantRunCollections(assistantRun);
  }
}

function syncAssistantRunCollections(assistantRun) {
  assistantRun.items.sort(compareTimelineChildren);
  assistantRun.reasoning = assistantRun.items.filter(
    (item) => item.type === 'reasoning',
  );
  assistantRun.outputs = assistantRun.items.filter(
    (item) => item.type === 'assistant_output',
  );
  assistantRun.tools = assistantRun.items.filter(
    (item) => item.type === 'tool_call',
  );
}

function pushActiveAssistantRun(timelineItems, assistantRun) {
  if (!assistantRun) {
    return;
  }
  syncAssistantRunCollections(assistantRun);
  timelineItems.push(stripTimelineSequence(assistantRun));
}

function historyMessageItem(message) {
  return {
    id: message.id ?? `history-${message.role}-${message.timestamp}`,
    type: 'message',
    message,
  };
}

function isAssistantRunEvent(event) {
  return [
    'run_started',
    'model_fallback_activated',
    RUN_EVENT_REASONING_DELTA,
    'reasoning',
    RUN_EVENT_TOOL_CALL_DELTA,
    'tool_call_started',
    RUN_EVENT_TOOL_CALL_STDOUT,
    RUN_EVENT_TOOL_CALL_STDERR,
    'tool_call_result',
    'subagent_session_started',
    RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
    'assistant_output',
    'run_completed',
    'run_failed',
    'run_cancelled',
  ].includes(event?.type);
}

function isStreamingDeltaRunEvent(eventType) {
  return [
    RUN_EVENT_REASONING_DELTA,
    RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
    RUN_EVENT_TOOL_CALL_DELTA,
  ].includes(eventType);
}

function appendCompressedStreamingRunEvent(sessionState, event) {
  const payloadKey = streamingDeltaPayloadKey(event.type);
  if (!payloadKey) {
    return;
  }

  const deltaText = event.payload?.[payloadKey];
  if (!deltaText) {
    return;
  }

  const lastEvent = sessionState.streamingRunEvents.at(-1);
  if (
    canMergeCompressedStreamingEvent(
      lastEvent,
      event,
      payloadKey,
      sessionState.streamingPhase,
    )
  ) {
    lastEvent.payload[payloadKey] =
      `${lastEvent.payload?.[payloadKey] ?? ''}${deltaText}`;
    lastEvent.sequence = firstSeenSequence(lastEvent.sequence, event.sequence);
    lastEvent._streamChunkCount = streamEventChunkCount(lastEvent) + 1;
    lastEvent._streamLatestSequence = streamEventLatestSequence(event);
    lastEvent.timestamp ??= event.timestamp;
    return;
  }

  sessionState.streamingRunEvents = [
    ...sessionState.streamingRunEvents,
    {
      ...event,
      payload: {
        ...event.payload,
      },
      _streamingPhase: sessionState.streamingPhase,
      _streamChunkCount: 1,
      _streamLatestSequence: streamEventLatestSequence(event),
    },
  ];
}

function canMergeCompressedStreamingEvent(
  existingEvent,
  incomingEvent,
  payloadKey,
  streamingPhase,
) {
  return (
    existingEvent?.type === incomingEvent.type &&
    existingEvent?.run_id === incomingEvent.run_id &&
    existingEvent?._streamingPhase === streamingPhase &&
    typeof existingEvent.payload?.[payloadKey] === 'string'
  );
}

function streamingDeltaPayloadKey(eventType) {
  if (eventType === RUN_EVENT_REASONING_DELTA) {
    return 'reasoning_delta';
  }
  if (eventType === RUN_EVENT_ASSISTANT_OUTPUT_DELTA) {
    return 'content_delta';
  }
  return null;
}

function streamingDeltaEventKey(event) {
  if (!Number.isFinite(event?.sequence)) {
    return null;
  }
  return `${event.run_id ?? 'run'}:${event.type}:${event.sequence}`;
}

function shouldShowStandaloneRunEvent(event) {
  return event?.type === 'user_message_persisted';
}

function compareRunEvents(left, right) {
  return (left.sequence ?? 0) - (right.sequence ?? 0);
}

function compareTimelineChildren(left, right) {
  return (left.sequence ?? 0) - (right.sequence ?? 0);
}

function firstSeenSequence(existingSequence, candidateSequence) {
  if (!Number.isFinite(existingSequence)) {
    return candidateSequence;
  }
  if (!Number.isFinite(candidateSequence)) {
    return existingSequence;
  }
  return Math.min(existingSequence, candidateSequence);
}

function hasToolCalls(message) {
  return Array.isArray(message?.tool_calls) && message.tool_calls.length > 0;
}

function previousTimelineItemIsUser(timelineItems) {
  const previousItem = timelineItems.at(-1);
  return (
    previousItem?.type === 'message' && previousItem.message?.role === 'user'
  );
}

function textFromRunEventMessage(event, key) {
  const message = event.payload?.message;
  if (message?.[key]) {
    return message[key];
  }
  return event.payload?.[key] ?? '';
}

function toolKeyFromToolCall(toolCall) {
  return toolKeyFromValues(toolCall?.id, toolCall?.index);
}

function toolKeyFromValues(id, index) {
  if (id !== undefined && id !== null && id !== '') {
    return `id-${id}`;
  }
  if (index !== undefined && index !== null) {
    return `index-${index}`;
  }
  return 'unknown';
}

function toolMatchesCall(tool, toolCall) {
  if (toolCall?.id && tool.toolCallId === toolCall.id) {
    return true;
  }
  return (
    !tool.toolCallId &&
    toolCall?.index !== undefined &&
    tool.index === toolCall.index
  );
}

function moreStableToolKey(existingKey, candidateKey) {
  if (candidateKey?.startsWith('id-')) {
    return candidateKey;
  }
  return existingKey;
}

function hasToolResultFailure(event) {
  return (
    Boolean(event.payload?.error) || hasResultFailure(event.payload?.result)
  );
}

function hasResultFailure(result) {
  const normalizedResult = parseResult(result);
  if (!normalizedResult || typeof normalizedResult !== 'object') {
    return false;
  }
  return Boolean(
    normalizedResult.error ||
    normalizedResult.ok === false ||
    normalizedResult.success === false ||
    ['error', 'failed'].includes(normalizedResult.status),
  );
}

function parseResult(result) {
  if (typeof result !== 'string') {
    return result;
  }
  try {
    return JSON.parse(result);
  } catch {
    return result;
  }
}

function normalizedTiming(timing) {
  if (!isPlainObject(timing)) {
    return null;
  }
  const durationMs = timing.duration_ms;
  if (!Number.isFinite(durationMs) || durationMs < 0) {
    return null;
  }
  return {
    ...timing,
    duration_ms: Math.max(0, Math.round(durationMs)),
  };
}

function timingDurationMs(timing) {
  return Number.isFinite(timing?.duration_ms) && timing.duration_ms >= 0
    ? timing.duration_ms
    : null;
}

function isPlainObject(value) {
  return Object.prototype.toString.call(value) === '[object Object]';
}

function updateStreamingItems(sessionState, event) {
  if (event.type === RUN_EVENT_ASSISTANT_OUTPUT_DELTA) {
    appendTextStreamingItem(sessionState, event, 'assistant', 'content_delta');
    return;
  }
  if (event.type === RUN_EVENT_REASONING_DELTA) {
    appendTextStreamingItem(
      sessionState,
      event,
      'reasoning',
      'reasoning_delta',
    );
    return;
  }
  if (event.type === RUN_EVENT_TOOL_CALL_DELTA) {
    appendToolCallStreamingItem(sessionState, event);
    return;
  }
  if (event.type === 'tool_call_started' || event.type === 'tool_call_result') {
    sessionState.streamingPhase += 1;
    return;
  }
  if (event.type === 'assistant_output') {
    sessionState.streamingItems = [];
  }
}

function appendTextStreamingItem(sessionState, event, itemType, payloadKey) {
  const contentDelta = event.payload?.[payloadKey];
  if (!contentDelta) {
    return;
  }
  const trailingItem = sessionState.streamingItems.at(-1);
  if (
    trailingItem?.type === itemType &&
    trailingItem.phase === sessionState.streamingPhase
  ) {
    trailingItem.content += contentDelta;
    trailingItem.sequence = event.sequence;
    trailingItem.timestamp ??= event.timestamp;
    return;
  }
  sessionState.streamingItems = [
    ...sessionState.streamingItems,
    {
      id: `${itemType}-${event.run_id ?? 'run'}-${event.sequence ?? sessionState.streamingItems.length}`,
      run_id: event.run_id,
      type: itemType,
      content: contentDelta,
      sequence: event.sequence,
      timestamp: event.timestamp,
      phase: sessionState.streamingPhase,
    },
  ];
}

function appendToolCallStreamingItem(sessionState, event) {
  const payload = event.payload ?? {};
  const toolCallId = payload.tool_call_id ?? payload.id;
  if (!toolCallId) {
    return;
  }
  const existingItem = sessionState.streamingItems.find(
    (item) =>
      item.type === 'tool_call' &&
      item.toolCallId === toolCallId &&
      item.phase === sessionState.streamingPhase,
  );
  if (existingItem) {
    existingItem.name += payload.name_delta ?? '';
    existingItem.argumentsText += payload.arguments_delta ?? '';
    existingItem.sequence = firstSeenSequence(
      existingItem.sequence,
      event.sequence,
    );
    existingItem.timestamp ??= event.timestamp;
    return;
  }
  sessionState.streamingItems = [
    ...sessionState.streamingItems,
    {
      id: `tool-call-${event.run_id ?? 'run'}-${toolCallId}`,
      run_id: event.run_id,
      type: 'tool_call',
      toolCallId,
      name: payload.name_delta ?? '',
      argumentsText: payload.arguments_delta ?? '',
      complete: false,
      sequence: event.sequence,
      timestamp: event.timestamp,
      phase: sessionState.streamingPhase,
    },
  ];
}

function stripTimelineSequence({ sequence: _sequence, ...item }) {
  return item;
}
