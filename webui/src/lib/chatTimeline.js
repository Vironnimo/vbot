import {
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
  RUN_EVENT_TOOL_CALL_STDERR,
  RUN_EVENT_TOOL_CALL_STDOUT,
} from './api.js';

const CHAT_STATUS_RUNNING = 'running';
const CHAT_STATUS_COMPLETED = 'completed';
const CHAT_STATUS_FAILED = 'failed';
const CHAT_STATUS_CANCELLED = 'cancelled';

const TERMINAL_RUN_EVENTS = new Set([
  'run_completed',
  'run_failed',
  'run_cancelled',
]);

function isRunActive(sessionState) {
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
  const liveItems = dropPersistedInactiveLiveRuns(
    liveTimelineItems(runEvents),
    sessionState.messages,
    sessionState.currentRun?.runId ?? null,
  );
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
    return trackedRunSourceWithoutUserAnchor(
      historyItems,
      liveItems,
      liveAssistantRun,
      activeRunId,
      sessionState.messages,
    );
  }

  const currentUserIndex = findMatchingHistoryUserIndex(
    sessionState.messages,
    activeUserEvent.payload.message,
  );
  if (currentUserIndex < 0) {
    return trackedRunSourceWithoutUserAnchor(
      historyItems,
      liveItems,
      liveAssistantRun,
      activeRunId,
      sessionState.messages,
    );
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

// An active run without a user_message_persisted event cannot be spliced into
// history by its user message. This happens for internal/automation runs — most
// notably the follow-up run a non-blocking sub-agent completion spawns, whose
// trigger is a hidden note, not a user message. When every assistant/tool
// message the live run produced is already persisted in history, the persisted
// copy is authoritative, so we drop the replayed live run to avoid rendering the
// same turn twice (the bug seen when refreshing while such a run is still
// running). When the live run carries output that is not yet persisted (e.g. a
// fresh run whose turn is not on the loaded history page), we keep it.
function trackedRunSourceWithoutUserAnchor(
  historyItems,
  liveItems,
  liveAssistantRun,
  activeRunId,
  messages,
) {
  if (!liveRunOutputPersistedInHistory(liveAssistantRun, messages)) {
    return [...historyItems, ...liveItems];
  }
  const remainingLiveItems = liveItems.filter(
    (item) => !matchesActiveRunTimelineItem(item, activeRunId),
  );
  return [...historyItems, ...remainingLiveItems];
}

function liveRunOutputPersistedInHistory(liveAssistantRun, messages) {
  const liveMessageIds = new Set();
  for (const event of liveAssistantRun?.events ?? []) {
    if (
      event?.type !== 'assistant_output' &&
      event?.type !== 'tool_call_result'
    ) {
      continue;
    }
    const messageId = event.payload?.message?.id;
    if (messageId) {
      liveMessageIds.add(messageId);
    }
  }
  if (liveMessageIds.size === 0) {
    return false;
  }

  const persistedIds = new Set(
    (messages ?? [])
      .filter(
        (message) => message?.role === 'assistant' || message?.role === 'tool',
      )
      .map((message) => message?.id)
      .filter(Boolean),
  );
  for (const messageId of liveMessageIds) {
    if (!persistedIds.has(messageId)) {
      return false;
    }
  }
  return true;
}

// Safety net: `runEvents` accumulates every run event appended while the tab is
// open and is only cleared by the next `loadHistory` for an idle session. When a
// follow-up run starts in the same session, the previous run's events remain
// next to the new active run's events. The snapshot model removes the original
// trigger (the WS replay-from-0 that re-injected already-completed runs on
// refresh), but this natural-flow case can still surface — most visibly the
// parent run that spawned a non-blocking sub-agent, whose events stay in
// `runEvents` until the next history load. liveTimelineItems builds a live
// block (plus user_message_persisted item) for every run_id, but
// selectTrackedRunTimelineSource only reconciles the single active run against
// history; every other run leaks in as a duplicate of its already-persisted
// turn. Drop the live items of any non-active run whose output is fully
// persisted in history. The active run is left untouched because it may still
// be streaming output that is not persisted yet; its own splice/anchor
// handling deduplicates it.
function dropPersistedInactiveLiveRuns(liveItems, messages, activeRunId) {
  const persistedRunIds = new Set();
  for (const item of liveItems) {
    if (item.type !== 'assistant_run') {
      continue;
    }
    const runId = item.runId ?? item.run_id;
    if (!runId || runId === activeRunId) {
      continue;
    }
    if (liveRunOutputPersistedInHistory(item, messages)) {
      persistedRunIds.add(runId);
    }
  }
  if (persistedRunIds.size === 0) {
    return liveItems;
  }
  return liveItems.filter(
    (item) => !liveItemBelongsToRuns(item, persistedRunIds),
  );
}

function liveItemBelongsToRuns(item, runIds) {
  if (item?.type === 'assistant_run') {
    return runIds.has(item.runId ?? item.run_id);
  }
  if (item?.type === 'event') {
    return runIds.has(item.event?.run_id);
  }
  return false;
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

function stripTimelineSequence({ sequence: _sequence, ...item }) {
  return item;
}
