import {
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
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
      streamingItems: [],
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
  return state.sessions[sessionKey(agent.id, agent.current_session_id)] ?? null;
}

export function loadHistory(sessionState, messages) {
  const activeRunEvents = isRunActive(sessionState)
    ? sessionState.runEvents
    : [];
  const activeStreamingItems = isRunActive(sessionState)
    ? sessionState.streamingItems
    : [];
  sessionState.messages = Array.isArray(messages) ? messages : [];
  sessionState.runEvents = activeRunEvents;
  sessionState.streamingItems = activeStreamingItems;
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
  sessionState.streamingItems = [];
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

export function finishRun(sessionState, event) {
  const type = event?.type;
  const status = event?.payload?.status;
  if (sessionState.currentRun) {
    sessionState.currentRun.status = status ?? terminalStatus(type);
  }
  sessionState.status = status ?? terminalStatus(type);
  sessionState.streamStatus = CHAT_STATUS_IDLE;
  sessionState.streamingItems = [];
  if (type === 'run_failed') {
    sessionState.error = event?.payload?.error ?? 'Run failed';
  }
  return sessionState;
}

export function highestRunEventSequence(sessionState) {
  return Math.max(
    0,
    ...(sessionState?.runEvents ?? [])
      .map((event) => event.sequence)
      .filter((sequence) => Number.isFinite(sequence)),
  );
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

export function restoreDequeuedMessage(sessionState, queuedMessage) {
  if (!queuedMessage) {
    return sessionState.queue;
  }
  sessionState.queue = [queuedMessage, ...sessionState.queue];
  return sessionState.queue;
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

  const historyItems = historyTimelineItems(sessionState.messages);
  const liveItems = liveTimelineItems(sessionState.runEvents);
  const reconciledItems = shouldSelectTrackedRunSource(sessionState)
    ? selectTrackedRunTimelineSource(sessionState, historyItems, liveItems)
    : [...historyItems, ...liveItems];

  return reconciledItems.map((item) => stripTimelineSequence(item));
}

function shouldSelectTrackedRunSource(sessionState) {
  return (
    Boolean(sessionState?.currentRun?.runId) &&
    Array.isArray(sessionState?.runEvents) &&
    sessionState.runEvents.length > 0
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

  for (const message of messages ?? []) {
    if (message?.role === 'user') {
      pushActiveAssistantRun(timelineItems, activeAssistantRun);
      activeAssistantRun = null;
      timelineItems.push(historyMessageItem(message));
      continue;
    }

    if (message?.role === 'assistant') {
      if (
        !activeAssistantRun &&
        (hasToolCalls(message) || previousTimelineItemIsUser(timelineItems))
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
        continue;
      }

      timelineItems.push(historyMessageItem(message));
      continue;
    }

    if (message?.role === 'tool' && activeAssistantRun) {
      appendHistoryToolResult(activeAssistantRun, message);
      continue;
    }

    pushActiveAssistantRun(timelineItems, activeAssistantRun);
    activeAssistantRun = null;
    timelineItems.push(historyMessageItem(message));
  }

  pushActiveAssistantRun(timelineItems, activeAssistantRun);
  return timelineItems;
}

function selectTrackedRunTimelineSource(sessionState, historyItems, liveItems) {
  const activeRunId = sessionState.currentRun?.runId ?? null;
  const liveAssistantRun = liveItems.find(
    (item) =>
      item.type === 'assistant_run' && matchesRunId(item.runId, activeRunId),
  );
  if (!liveAssistantRun) {
    return [...historyItems, ...liveItems];
  }

  const activeUserEvent = activeRunUserEvent(
    sessionState.runEvents,
    activeRunId,
  );
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
  const assistantRun = createAssistantRunItem({
    id: `assistant-run-${runKey}`,
    runId: firstEvent.run_id,
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

  if (TERMINAL_RUN_EVENTS.has(event.type)) {
    assistantRun.endTimestamp = event.timestamp ?? assistantRun.endTimestamp;
    assistantRun.status = event.payload?.status ?? terminalStatus(event.type);
    assistantRun.terminalEvent = event;
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

  if (event.type === 'tool_call_result') {
    mergeToolResult(assistantRun, event);
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

  if (message.content) {
    appendTextSection(assistantRun, {
      type: 'assistant_output',
      content: message.content,
      message,
      streaming: false,
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
    },
  });
  assistantRun.status = hasResultFailure(message.content)
    ? CHAT_STATUS_FAILED
    : CHAT_STATUS_COMPLETED;
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
    return mergeableStreamingDraftAcrossFinalizedRows(
      assistantRun,
      type,
      content,
      message,
    );
  }

  return null;
}

function mergeableStreamingDraftAcrossFinalizedRows(
  assistantRun,
  type,
  content,
  message,
) {
  const draftIndex = assistantRun.items.findLastIndex(
    (item) =>
      item.type === type &&
      item.streaming &&
      (item.content === content ||
        messageSharesInterveningToolCall(message, item, assistantRun)),
  );
  if (draftIndex < 0) {
    return null;
  }

  const interveningItems = assistantRun.items.slice(draftIndex + 1);
  const hasFinalSameTypeAfterDraft = interveningItems.some(
    (item) => item.type === type && !item.streaming,
  );
  return hasFinalSameTypeAfterDraft ? null : assistantRun.items[draftIndex];
}

function messageSharesInterveningToolCall(message, draftItem, assistantRun) {
  const messageToolKeys = new Set(
    (message?.tool_calls ?? []).map((toolCall, index) =>
      toolKeyFromToolCall({ index, ...toolCall }),
    ),
  );
  if (messageToolKeys.size === 0) {
    return false;
  }

  return assistantRun.items.some(
    (item) =>
      item.type === 'tool_call' &&
      (item.sequence ?? 0) > (draftItem.sequence ?? 0) &&
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
  tool.partialArgumentsText = null;
  tool.startedEvent = event;
  tool.status = tool.resultEvent ? tool.status : CHAT_STATUS_RUNNING;
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
  tool.status = hasToolResultFailure(event) ? CHAT_STATUS_FAILED : 'success';
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
    partialArgumentsText: null,
    result: undefined,
    toolCall: null,
    startedEvent: null,
    resultEvent: null,
    events: [],
  };
  assistantRun.items.push(tool);
  syncAssistantRunCollections(assistantRun);
  return tool;
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
    RUN_EVENT_REASONING_DELTA,
    'reasoning',
    RUN_EVENT_TOOL_CALL_DELTA,
    'tool_call_started',
    'tool_call_result',
    RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
    'assistant_output',
    'run_completed',
    'run_failed',
    'run_cancelled',
  ].includes(event?.type);
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
  if (trailingItem?.type === itemType) {
    trailingItem.content += contentDelta;
    trailingItem.sequence = event.sequence;
    return;
  }
  sessionState.streamingItems = [
    ...sessionState.streamingItems,
    {
      id: `${itemType}-${event.run_id ?? 'run'}-${event.sequence ?? sessionState.streamingItems.length}`,
      type: itemType,
      content: contentDelta,
      sequence: event.sequence,
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
    (item) => item.type === 'tool_call' && item.toolCallId === toolCallId,
  );
  if (existingItem) {
    existingItem.name += payload.name_delta ?? '';
    existingItem.argumentsText += payload.arguments_delta ?? '';
    return;
  }
  sessionState.streamingItems = [
    ...sessionState.streamingItems,
    {
      id: `tool-call-${event.run_id ?? 'run'}-${toolCallId}`,
      type: 'tool_call',
      toolCallId,
      name: payload.name_delta ?? '',
      argumentsText: payload.arguments_delta ?? '',
      complete: false,
      sequence: event.sequence,
    },
  ];
}

function stripTimelineSequence({ sequence: _sequence, ...item }) {
  return item;
}
