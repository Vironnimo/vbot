import {
  mergeTimelineItems,
  historyMessageItem,
  isStreamingDeltaEvent,
  RUN_HISTORY_CONTENT_ROLES,
  createAssistantRunItem,
  syncAssistantRunCollections,
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_CANCELLED,
  CHAT_STATUS_INTERRUPTED,
  TERMINAL_RUN_EVENTS,
  isRunActive,
  normalizedIterationCount,
} from './model.js';
import {
  historyTimelineItems,
  appendHistoryAssistantMessage,
  appendHistoryToolResult,
} from './history.js';
import { appendLiveRunEvent } from './live.js';
import { markPendingToolsCancelled } from './runChildren.js';

export function selectTrackedRunTimelineSource(
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
    return mergeTimelineItems(historyItems, liveItems);
  }
  liveAssistantRun.startTimestamp =
    sessionState.currentRun?.startedAt ?? liveAssistantRun.startTimestamp;

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

  const activeUserItem = historyMessageItem(
    sessionState.messages[currentUserIndex],
  );
  const currentLiveErrors = remainingLiveItems.filter(
    (item) => item.liveErrorRunId === activeRunId,
  );
  const prefixHistoryItems = mergeTimelineItems(
    historyTimelineItems(prefixMessages),
    remainingLiveItems.filter((item) => item.liveErrorRunId !== activeRunId),
  );
  const trailingHistoryItems = historyTimelineItems(trailingMessages);

  if (
    isTrackedRunTerminal(sessionState, liveAssistantRun) &&
    hasPersistedAssistantTurn(currentTurnMessages)
  ) {
    if (!hasPersistedRunSummary(currentTurnMessages, activeRunId)) {
      // A History page loaded while the Run was still active can contain only
      // the prefix persisted at that instant. Do not let the later terminal
      // event turn that stale prefix into an authoritative completed Run: the
      // live projection may contain several newer Assistant/Tool messages.
      // History is safe to select only when it covers every stable live output
      // (or replay contains neither an output message nor a streaming draft).
      if (!historyCoversLiveRunOutput(liveAssistantRun, currentTurnMessages)) {
        return [
          ...prefixHistoryItems,
          activeUserItem,
          liveAssistantRun,
          ...trailingHistoryItems,
          ...currentLiveErrors,
        ];
      }
      const currentTurnHistoryItems = historyTimelineItems(currentTurnMessages);
      applyLiveTerminalStateToHistory(
        currentTurnHistoryItems,
        liveAssistantRun,
        activeRunId,
      );
      return [
        ...prefixHistoryItems,
        ...currentTurnHistoryItems,
        ...trailingHistoryItems,
        ...currentLiveErrors,
      ];
    }
    return mergeTimelineItems(historyItems, remainingLiveItems);
  }

  return [
    ...prefixHistoryItems,
    activeUserItem,
    liveAssistantRun,
    ...trailingHistoryItems,
    ...currentLiveErrors,
  ];
}

function historyCoversLiveRunOutput(liveAssistantRun, messages) {
  const liveEvents = liveAssistantRun?.events ?? [];
  if (liveEvents.some((event) => isStreamingDeltaEvent(event?.type))) {
    return false;
  }
  const hasStableOutputMessage = liveEvents.some(
    (event) =>
      ['assistant_output', 'tool_call_result'].includes(event?.type) &&
      Boolean(event.payload?.message?.id),
  );
  if (!hasStableOutputMessage) {
    return true;
  }
  return liveRunOutputPersistedInHistory(liveAssistantRun, messages);
}

// An active run without a user_message_persisted event cannot be spliced into
// history by its user message. This happens for internal/automation runs — most
// notably the follow-up run a background sub-agent completion spawns, whose
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
  const liveMessageIds = new Set(
    liveAssistantRun.events
      .map((event) => event.payload?.message?.id)
      .filter(Boolean),
  );
  const overlapIndex = (messages ?? []).findIndex(
    (message) =>
      RUN_HISTORY_CONTENT_ROLES.has(message.role) &&
      liveMessageIds.has(message.id),
  );
  const currentLiveItems = liveItems.filter(
    (item) =>
      matchesActiveRunTimelineItem(item, activeRunId) ||
      item.liveErrorRunId === activeRunId,
  );
  const remainingLiveItems = liveItems.filter(
    (item) => !currentLiveItems.includes(item),
  );
  const currentLiveErrors = currentLiveItems.filter(
    (item) => item.liveErrorRunId === activeRunId,
  );
  if (hasPersistedRunSummary(messages, activeRunId)) {
    return mergeTimelineItems(historyItems, remainingLiveItems);
  }
  if (overlapIndex < 0) {
    return liveRunOutputPersistedInHistory(liveAssistantRun, messages)
      ? mergeTimelineItems(historyItems, remainingLiveItems)
      : [
          ...mergeTimelineItems(historyItems, remainingLiveItems),
          ...currentLiveItems,
        ];
  }

  // A note-triggered Run has no visible User anchor. Its persisted overlap
  // locates the Assistant/Tool segment; retain the unseen replay prefix and
  // overlay live children instead of declaring the entire Run persisted.
  let start = overlapIndex;
  let end = overlapIndex + 1;
  while (start > 0 && RUN_HISTORY_CONTENT_ROLES.has(messages[start - 1].role))
    start -= 1;
  while (
    end < messages.length &&
    RUN_HISTORY_CONTENT_ROLES.has(messages[end].role)
  )
    end += 1;
  const historyRun = createAssistantRunItem({
    id: liveAssistantRun.id,
    runId: activeRunId,
    source: 'history',
  });
  for (const message of messages.slice(start, end)) {
    if (message.role === 'assistant')
      appendHistoryAssistantMessage(historyRun, message);
    else if (message.role === 'tool')
      appendHistoryToolResult(historyRun, message);
    else
      appendLiveRunEvent(historyRun, {
        type: 'compaction_completed',
        sequence: historyRun.items.length,
        timestamp: message.timestamp,
        payload: { message },
      });
  }
  const items = [...historyRun.items];
  let insertionIndex = items.length;
  for (const child of [...liveAssistantRun.items].reverse()) {
    const matchingIndex = items.findIndex((historyChild) =>
      timelineChildrenMatch(historyChild, child),
    );
    if (matchingIndex < 0) {
      items.splice(insertionIndex, 0, child);
      continue;
    }
    items[matchingIndex] = mergeTimelineChild(items[matchingIndex], child);
    insertionIndex = matchingIndex;
  }
  const mergedRun = {
    ...liveAssistantRun,
    timestamp: messages[start].timestamp ?? liveAssistantRun.timestamp,
    items: items.map((item, sequence) => ({ ...item, sequence })),
  };
  syncAssistantRunCollections(mergedRun);
  return mergeTimelineItems(
    [
      ...historyTimelineItems(messages.slice(0, start)),
      mergedRun,
      ...currentLiveErrors,
      ...historyTimelineItems(messages.slice(end)),
    ],
    remainingLiveItems,
  );
}

function timelineChildrenMatch(historyChild, liveChild) {
  if (historyChild.type !== liveChild.type) return false;
  if (historyChild.type === 'tool_call')
    return (
      Boolean(historyChild.toolCallId) &&
      historyChild.toolCallId === liveChild.toolCallId
    );
  if (historyChild.type === 'compaction_separator')
    return (
      Boolean(historyChild.message?.id) &&
      historyChild.message.id === liveChild.message?.id
    );
  const historyIds = new Set(
    (historyChild.messages ?? []).map((message) => message.id).filter(Boolean),
  );
  return (liveChild.events ?? []).some((event) =>
    historyIds.has(event.payload?.message?.id),
  );
}

function mergeTimelineChild(historyChild, liveChild) {
  if (
    historyChild.type === 'tool_call' &&
    historyChild.resultEvent &&
    !liveChild.resultEvent
  ) {
    return {
      ...historyChild,
      stdout: liveChild.stdout || historyChild.stdout,
      stderr: liveChild.stderr || historyChild.stderr,
    };
  }
  return { ...historyChild, ...liveChild };
}

function liveRunOutputPersistedInHistory(liveAssistantRun, messages) {
  return runOutputPersistedInHistory(liveAssistantRun?.events, messages);
}

function runOutputPersistedInHistory(events, messages) {
  const liveMessageIds = new Set();
  for (const event of events ?? []) {
    if (
      event?.type !== 'assistant_output' &&
      event?.type !== 'tool_call_result' &&
      event?.type !== 'compaction_completed'
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
        (message) =>
          message?.role === 'assistant' ||
          message?.role === 'tool' ||
          message?.role === 'compaction_checkpoint',
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

export function runProjectionPersistedInHistory(runEvents, messages, runId) {
  if (!runId) {
    return false;
  }
  if (persistedRunSummaryIds(messages).has(runId)) {
    return true;
  }
  return runOutputPersistedInHistory(
    (runEvents ?? []).filter((event) => event?.run_id === runId),
    messages,
  );
}

// Safety net: `runEvents` accumulates every run event appended while the tab is
// open and is only cleared by the next `loadHistory` for an idle session. When a
// follow-up run starts in the same session, the previous run's events remain
// next to the new active run's events. The snapshot model removes the original
// trigger (the WS replay-from-0 that re-injected already-completed runs on
// refresh), but this natural-flow case can still surface — most visibly the
// parent run that spawned a background sub-agent, whose events stay in
// `runEvents` until the next history load. liveTimelineItems builds a live
// block (plus user_message_persisted item) for every run_id, but
// selectTrackedRunTimelineSource only reconciles the single active run against
// history; every other run leaks in as a duplicate of its already-persisted
// turn. Drop the live items of any non-active run that History has finalized
// with a Run Summary, even when the bounded replay retained no output events;
// matching persisted output ids remain the fallback before a summary loads.
// The active run is left untouched because it may still be streaming output
// that is not persisted yet; its own splice/anchor handling deduplicates it.
export function dropPersistedInactiveLiveRuns(
  liveItems,
  messages,
  activeRunId,
) {
  const summarizedRunIds = persistedRunSummaryIds(messages);
  const persistedRunIds = new Set();
  for (const item of liveItems) {
    if (item.type !== 'assistant_run') {
      continue;
    }
    const runId = item.runId ?? item.run_id;
    if (!runId || runId === activeRunId) {
      continue;
    }
    if (
      summarizedRunIds.has(runId) ||
      liveRunOutputPersistedInHistory(item, messages)
    ) {
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

// Event-level counterpart of dropPersistedInactiveLiveRuns, used by
// `loadHistory` to shrink `sessionState.runEvents` instead of only hiding the
// duplicates at render time (handoff3 B10). Events of a summarized non-active
// run, or one whose output ids are fully persisted in freshly loaded History,
// would be dropped by the render-time predicate anyway. Removing them from the
// retained array changes nothing visually while keeping it from growing across
// navigations and reloads during an active run.
export function pruneRunEventsPersistedInHistory(
  runEvents,
  messages,
  activeRunId,
) {
  const eventsByRun = new Map();
  for (const event of runEvents ?? []) {
    const runId = event?.run_id;
    if (!runId || runId === activeRunId) {
      continue;
    }
    if (!eventsByRun.has(runId)) {
      eventsByRun.set(runId, []);
    }
    eventsByRun.get(runId).push(event);
  }

  const prunedRunIds = new Set();
  for (const [runId, events] of eventsByRun) {
    if (runProjectionPersistedInHistory(events, messages, runId)) {
      prunedRunIds.add(runId);
    }
  }
  if (prunedRunIds.size === 0) {
    return runEvents ?? [];
  }
  return (runEvents ?? []).filter((event) => !prunedRunIds.has(event?.run_id));
}

function persistedRunSummaryIds(messages) {
  return new Set(
    (messages ?? [])
      .filter(
        (message) =>
          message?.role === 'run_summary' &&
          typeof message.run_id === 'string' &&
          message.run_id.length > 0,
      )
      .map((message) => message.run_id),
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
    [
      CHAT_STATUS_COMPLETED,
      CHAT_STATUS_FAILED,
      CHAT_STATUS_CANCELLED,
      CHAT_STATUS_INTERRUPTED,
    ].includes(sessionState.currentRun?.status)
  );
}

function hasPersistedAssistantTurn(messages) {
  return (messages ?? []).some((message) =>
    ['assistant', 'tool'].includes(message?.role),
  );
}

function hasPersistedRunSummary(messages, runId) {
  return (messages ?? []).some(
    (message) =>
      message?.role === 'run_summary' && matchesRunId(message.run_id, runId),
  );
}

function applyLiveTerminalStateToHistory(
  historyItems,
  liveAssistantRun,
  runId,
) {
  const historyAssistantRun = (historyItems ?? []).find(
    (item) => item?.type === 'assistant_run',
  );
  if (!historyAssistantRun) {
    return;
  }

  historyAssistantRun.runId = runId;
  historyAssistantRun.run_id = runId;
  historyAssistantRun.status = liveAssistantRun.status;
  historyAssistantRun.timing =
    liveAssistantRun.timing ?? historyAssistantRun.timing;
  historyAssistantRun.startTimestamp =
    liveAssistantRun.startTimestamp ?? historyAssistantRun.startTimestamp;
  historyAssistantRun.endTimestamp =
    liveAssistantRun.endTimestamp ?? historyAssistantRun.endTimestamp;
  historyAssistantRun.durationMs =
    liveAssistantRun.durationMs ?? historyAssistantRun.durationMs;
  if (normalizedIterationCount(liveAssistantRun.iterationCount) !== null) {
    historyAssistantRun.iterationCount = liveAssistantRun.iterationCount;
  }
  historyAssistantRun.terminalEvent = liveAssistantRun.terminalEvent;
  if (liveAssistantRun.terminalEvent?.type === 'run_cancelled') {
    markPendingToolsCancelled(
      historyAssistantRun,
      liveAssistantRun.terminalEvent,
    );
  }
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
