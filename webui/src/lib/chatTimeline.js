import {
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_CHANGE_STATS,
  RUN_EVENT_PROVIDER_HEARTBEAT,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
  RUN_EVENT_TOOL_CALL_STDERR,
  RUN_EVENT_TOOL_CALL_STDOUT,
} from './api.js';
import { isPlainObject } from './values.js';

const CHAT_STATUS_RUNNING = 'running';
const CHAT_STATUS_COMPLETED = 'completed';
const CHAT_STATUS_FAILED = 'failed';
const CHAT_STATUS_PARTIAL = 'partial';
const CHAT_STATUS_CANCELLED = 'cancelled';
const CHAT_STATUS_INTERRUPTED = 'interrupted';

const RUN_HISTORY_CONTENT_ROLES = new Set([
  'assistant',
  'tool',
  'compaction_checkpoint',
]);

const TERMINAL_RUN_EVENTS = new Set([
  'run_completed',
  'run_failed',
  'run_cancelled',
  'run_interrupted',
]);
const PROVIDER_PROGRESS_RUN_EVENTS = new Set([
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
  'reasoning',
  'assistant_output',
  'tool_call_started',
]);

function isRunActive(sessionState) {
  return sessionState?.status === CHAT_STATUS_RUNNING;
}

export function visibleTimelineItemsForRender(sessionState) {
  if (!sessionState) {
    return [];
  }

  return buildVisibleTimelineItems(sessionState, [
    ...(sessionState.runEvents ?? []),
    ...(sessionState.streamingRunEvents ?? []),
  ]);
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
  if (child.type === 'compaction_separator') {
    const summaryLength =
      typeof child.message?.content === 'string'
        ? child.message.content.length
        : 0;
    return `${chunkCount}:${latestSequence ?? ''}:${child.status ?? ''}:${child.contextTokensBefore ?? ''}:${child.contextTokensAfter ?? ''}:${summaryLength}`;
  }
  return `${chunkCount}:${latestSequence ?? ''}:${contentLength}`;
}

// Per-session memo of projected assistant_run items, keyed by run. A run
// whose group contains a terminal event and no retained streaming delta can no
// longer change: non-delta run events are appended exactly once
// (appendRunEvent dedups by run_id + sequence) and never mutated. A terminal
// Run may temporarily retain deltas while canonical output is still in flight,
// so that group stays uncached until History or the next Run clears them.
// Reusing every other terminal Run's projection across the ≤33 ms streaming
// flushes keeps the per-flush rebuild cost bound to the active Run instead of
// growing with Session age (handoff3 B10).
const liveRunProjectionCachesBySession = new WeakMap();

function liveRunProjectionCache(sessionState) {
  let cache = liveRunProjectionCachesBySession.get(sessionState);
  if (!cache) {
    cache = new Map();
    liveRunProjectionCachesBySession.set(sessionState, cache);
  }
  return cache;
}

function buildVisibleTimelineItems(sessionState, runEvents) {
  if (!sessionState) {
    return [];
  }

  const historyItems = historyTimelineItems(sessionState.messages);
  const liveItems = dropPersistedInactiveLiveRuns(
    liveTimelineItems(runEvents, liveRunProjectionCache(sessionState)),
    sessionState.messages,
    sessionState.currentRun?.runId ?? null,
  );
  applyCurrentRunIterationCount(liveItems, sessionState.currentRun);
  const reconciledItems = shouldSelectTrackedRunSource(sessionState, runEvents)
    ? selectTrackedRunTimelineSource(
        sessionState,
        historyItems,
        liveItems,
        runEvents,
      )
    : mergeTimelineItems(historyItems, liveItems);

  const persistedMessageIds = new Set(
    reconciledItems
      .filter((item) => item.type === 'message' && !item.liveErrorRunId)
      .map((item) => item.id),
  );
  const visibleItems = reconciledItems.filter(
    (item) => !item.liveErrorRunId || !persistedMessageIds.has(item.id),
  );
  return visibleItems.flatMap((item) =>
    isCompactionOnlyRunItem(item)
      ? item.items.map(stripTimelineSequence)
      : [stripTimelineSequence(item)],
  );
}

// Insert retained Run groups into canonical History without sorting History
// itself or separating a live User message from its Assistant output.
function mergeTimelineItems(historyItems, liveItems) {
  const result = [...historyItems];
  for (let index = 0; index < liveItems.length; index += 1) {
    const item = liveItems[index];
    const group = [item];
    const next = liveItems[index + 1];
    if (
      item.event?.type === 'user_message_persisted' &&
      next?.type === 'assistant_run' &&
      next.runId === item.event.run_id
    ) {
      group.push(next);
      index += 1;
    }
    const timestamp = timelineItemTimestamp(item);
    const insertionIndex =
      timestamp === null
        ? -1
        : result.findIndex((existing) => {
            const existingTimestamp = timelineItemTimestamp(existing);
            return existingTimestamp !== null && existingTimestamp > timestamp;
          });
    result.splice(
      insertionIndex < 0 ? result.length : insertionIndex,
      0,
      ...group,
    );
  }
  return result;
}

function timelineItemTimestamp(item) {
  return timestampToMs(
    item.message?.timestamp ?? item.event?.timestamp ?? item.timestamp,
  );
}

// A standalone Compaction Run emits nothing but Compaction events, so its run
// block would wrap the exact separator the automatic in-run Compaction renders
// bare. Dissolving it at the render boundary makes both triggers look
// identical. A failed or cancelled run keeps its block (the aborted
// placeholder is removed, leaving an empty block that carries the failure
// status), and an in-run auto Compaction never qualifies because its Run
// carries reasoning/output/tool children besides the separator.
function isCompactionOnlyRunItem(item) {
  return (
    item?.type === 'assistant_run' &&
    (item.status === 'running' || item.status === 'completed') &&
    (item.items ?? []).length > 0 &&
    item.items.every((child) => child?.type === 'compaction_separator')
  );
}

function applyCurrentRunIterationCount(liveItems, currentRun) {
  const iterationCount = normalizedIterationCount(currentRun?.iterationCount);
  if (iterationCount === null || !currentRun?.runId) {
    return;
  }
  const assistantRun = (liveItems ?? []).find(
    (item) => item?.type === 'assistant_run' && item.runId === currentRun.runId,
  );
  if (assistantRun) {
    assistantRun.iterationCount = iterationCount;
  }
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
  if (eventType === 'run_interrupted') {
    return CHAT_STATUS_INTERRUPTED;
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
        durationMs: message?.usage?.compaction_duration_ms ?? null,
      });
      previousVisibleRole = 'compaction_checkpoint';
      continue;
    }

    if (message?.role === 'agent_takeover') {
      pushActiveAssistantRun(timelineItems, activeAssistantRun);
      activeAssistantRun = null;
      timelineItems.push({
        id: `takeover-${message.id ?? message.timestamp}`,
        type: 'takeover_separator',
        timestamp: message.timestamp,
        message,
      });
      previousVisibleRole = 'agent_takeover';
      continue;
    }

    if (message?.role === 'run_summary') {
      if (activeAssistantRun) {
        appendHistoryRunSummary(activeAssistantRun, message);
        pushActiveAssistantRun(timelineItems, activeAssistantRun);
        activeAssistantRun = null;
      } else if (
        message.status === 'cancelled' ||
        message.status === 'interrupted'
      ) {
        // A terminal run with no visible output has only its summary as a
        // durable trace. Render a bare status row instead of leaving a hole.
        timelineItems.push(
          terminalRunSummaryItem(message, timelineItems.length),
        );
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
function dropPersistedInactiveLiveRuns(liveItems, messages, activeRunId) {
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

function liveTimelineItems(runEvents, projectionCache = null) {
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
          item: {
            ...historyMessageItem(message),
            liveErrorRunId: event.run_id,
          },
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

  if (projectionCache) {
    for (const runKey of projectionCache.keys()) {
      if (!runGroups.has(runKey)) {
        projectionCache.delete(runKey);
      }
    }
  }

  return timelineEntries
    .sort((left, right) => left.order - right.order)
    .flatMap((entry) => liveTimelineEntryItems(entry, projectionCache));
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

function liveTimelineEntryItems(entry, projectionCache) {
  if (entry.kind === 'standalone') {
    return [entry.item];
  }

  // Iteration telemetry is non-visual. A sparse WebSocket replay containing
  // only Usage or change statistics must not fabricate an empty Assistant Run
  // row.
  if (
    entry.events.every(
      (event) =>
        event.type === 'model_step_usage' ||
        event.type === RUN_EVENT_CHANGE_STATS,
    )
  ) {
    return [entry.userItem].filter(Boolean);
  }

  const assistantRun = projectedLiveAssistantRunItem(entry, projectionCache);
  return [entry.userItem, assistantRun].filter(Boolean);
}

function projectedLiveAssistantRunItem(entry, projectionCache) {
  const cacheable =
    Boolean(projectionCache) &&
    entry.events.some((event) => TERMINAL_RUN_EVENTS.has(event?.type)) &&
    !entry.events.some((event) => isStreamingDeltaEvent(event?.type));
  if (cacheable) {
    const cached = projectionCache.get(entry.runKey);
    if (cached && cached.eventCount === entry.events.length) {
      return cached.assistantRun;
    }
  }

  const assistantRun = buildLiveAssistantRunItem(entry.runKey, entry.events);
  if (cacheable) {
    projectionCache.set(entry.runKey, {
      eventCount: entry.events.length,
      assistantRun,
    });
  }
  return assistantRun;
}

function isStreamingDeltaEvent(eventType) {
  return [
    RUN_EVENT_REASONING_DELTA,
    RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
    RUN_EVENT_TOOL_CALL_DELTA,
  ].includes(eventType);
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
    providerHeartbeat: null,
    iterationCount: source === 'live' ? 0 : null,
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

  if (
    event.type !== RUN_EVENT_REASONING_DELTA &&
    event.type !== RUN_EVENT_PROVIDER_HEARTBEAT
  ) {
    freezeStreamingReasoningEstimates(assistantRun, event.timestamp);
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

  if (event.type === 'compaction_started') {
    assistantRun.items.push({
      id: `compaction-start-${assistantRun.id}-${event.sequence ?? assistantRun.items.length}`,
      type: 'compaction_separator',
      status: CHAT_STATUS_RUNNING,
      sequence: event.sequence ?? assistantRun.items.length,
      timestamp: event.timestamp,
      contextTokensBefore: event.payload?.context_tokens_before,
      contextTokensAfter: null,
      message: null,
      events: [event],
    });
    syncAssistantRunCollections(assistantRun);
    return;
  }

  if (event.type === 'compaction_aborted') {
    const runningIndex = assistantRun.items.findLastIndex(
      (item) =>
        item.type === 'compaction_separator' &&
        item.status === CHAT_STATUS_RUNNING,
    );
    if (runningIndex >= 0) {
      assistantRun.items.splice(runningIndex, 1);
      syncAssistantRunCollections(assistantRun);
    }
    return;
  }

  if (event.type === 'compaction_completed') {
    const message = event.payload?.message;
    const runningItem = [...assistantRun.items]
      .reverse()
      .find(
        (item) =>
          item.type === 'compaction_separator' &&
          item.status === CHAT_STATUS_RUNNING,
      );
    const completedItem = {
      id:
        runningItem?.id ??
        `compaction-${message?.id ?? event.sequence ?? assistantRun.items.length}`,
      type: 'compaction_separator',
      status: CHAT_STATUS_COMPLETED,
      sequence:
        runningItem?.sequence ?? event.sequence ?? assistantRun.items.length,
      timestamp: message?.timestamp ?? event.timestamp,
      contextTokensBefore:
        event.payload?.context_tokens_before ??
        message?.usage?.context_tokens_before ??
        null,
      contextTokensAfter:
        event.payload?.context_tokens_after ??
        message?.usage?.context_tokens_after ??
        null,
      durationMs:
        event.payload?.duration_ms ??
        message?.usage?.compaction_duration_ms ??
        null,
      message,
      events: [...(runningItem?.events ?? []), event],
    };
    if (runningItem) {
      Object.assign(runningItem, completedItem);
    } else {
      assistantRun.items.push(completedItem);
    }
    syncAssistantRunCollections(assistantRun);
    return;
  }

  if (event.type === RUN_EVENT_PROVIDER_HEARTBEAT) {
    assistantRun.providerHeartbeat = {
      idleSeconds: Number.isFinite(event.payload?.idle_seconds)
        ? event.payload.idle_seconds
        : null,
      timestamp: event.timestamp ?? null,
    };
    return;
  }

  if (PROVIDER_PROGRESS_RUN_EVENTS.has(event.type)) {
    assistantRun.providerHeartbeat = null;
  }

  if (event.type === 'model_step_usage') {
    const iterationCount = normalizedIterationCount(
      event.payload?.iteration_count,
    );
    if (iterationCount !== null) {
      assistantRun.iterationCount = iterationCount;
    }
    return;
  }

  // Live git-style change statistics streamed after each dispatched Tool
  // round. Carries the same validated shape as the terminal change_stats;
  // an all-zero object is meaningful (edits reverted to their baseline) and
  // retires an earlier nonzero total instead of falling back to sums.
  if (event.type === RUN_EVENT_CHANGE_STATS) {
    if (isPlainObject(event.payload?.change_stats)) {
      assistantRun.changeStats = event.payload.change_stats;
    }
    return;
  }

  if (TERMINAL_RUN_EVENTS.has(event.type)) {
    assistantRun.items = assistantRun.items.filter(
      (item) =>
        (item.type !== 'compaction_separator' ||
          item.status !== CHAT_STATUS_RUNNING) &&
        (event.type !== 'run_interrupted' ||
          item.type !== 'tool_call' ||
          !item.streaming),
    );
    syncAssistantRunCollections(assistantRun);
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
    const iterationCount = normalizedIterationCount(
      event.payload?.iteration_count,
    );
    if (iterationCount !== null) {
      assistantRun.iterationCount = iterationCount;
    }
    assistantRun.terminalEvent = event;
    if (isPlainObject(event.payload?.change_stats)) {
      assistantRun.changeStats = event.payload.change_stats;
    }
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
      durationMs: event.payload?.message?.reasoning_timing?.duration_ms ?? null,
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
    if (message?.interrupted) {
      assistantRun.items = assistantRun.items.filter(
        (item) => item.type !== 'tool_call' || !item.streaming,
      );
      syncAssistantRunCollections(assistantRun);
    }
    if (message?.reasoning) {
      appendTextSection(assistantRun, {
        type: 'reasoning',
        content: message.reasoning,
        durationMs: message.reasoning_timing?.duration_ms ?? null,
        event,
        streaming: false,
      });
    }

    appendTextSection(assistantRun, {
      type: 'assistant_output',
      content: textFromRunEventMessage(event, 'content'),
      event,
      streaming: false,
      interrupted: Boolean(message?.interrupted),
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
      durationMs: message.reasoning_timing?.duration_ms ?? null,
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
      interrupted: Boolean(message.interrupted),
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
      display: message.tool_display,
    },
  });
  // A per-tool user cancel is not a run failure — the run continued past it.
  // (Only relevant for runs without a run_summary; the summary overrides.)
  const runFailed =
    hasResultFailure(message.content) &&
    !toolResultCancelledByUser(message.content);
  assistantRun.status = runFailed ? CHAT_STATUS_FAILED : CHAT_STATUS_COMPLETED;
}

// A run row built from a cancelled/interrupted run_summary alone (no
// assistant/tool anchor): status and timing, with no children. This keeps a
// durable trace when the terminal Run produced no visible output.
function terminalRunSummaryItem(message, sequence) {
  const assistantRun = createAssistantRunItem({
    id: `history-run-summary-${message.id ?? message.timestamp ?? sequence}`,
    runId: message.run_id ?? null,
    source: 'history',
    sequence,
    timestamp: message.timestamp,
  });
  appendHistoryRunSummary(assistantRun, message);
  syncAssistantRunCollections(assistantRun);
  return stripTimelineSequence(assistantRun);
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
  const iterationCount = normalizedIterationCount(message?.iteration_count);
  if (iterationCount !== null) {
    assistantRun.iterationCount = iterationCount;
  }
  if (isPlainObject(message?.change_stats)) {
    assistantRun.changeStats = message.change_stats;
  }
  assistantRun.runSummaryMessage = message;
  if (assistantRun.status === CHAT_STATUS_CANCELLED) {
    // Live run_cancelled events settle every still-open Tool row. History must
    // project the same terminal truth after those transient events are pruned;
    // otherwise a cancelled foreground Sub-Agent is rebuilt as "starting"
    // forever even though both Parent and Child Runs already stopped.
    markPendingToolsCancelled(assistantRun, {
      type: 'run_cancelled',
      timestamp: message.timestamp,
      payload: { status: CHAT_STATUS_CANCELLED },
    });
  }
}

// Reasoning has no explicit end event: any other live Run event (except the
// idle heartbeat) means the streamed reasoning draft stopped growing, so its
// ticking header freezes at that boundary instead of counting on through the
// following Tool Calls. The draft itself stays open for merging; the next
// stable boundary replaces the frozen estimate with the measured server-side
// span via appendTextSection.
function freezeStreamingReasoningEstimates(assistantRun, endedTimestamp) {
  const endedMs = timestampToMs(endedTimestamp);
  for (const item of assistantRun.items) {
    if (
      item.type !== 'reasoning' ||
      !item.streaming ||
      item.durationEstimateMs !== null ||
      item.durationMs !== null
    ) {
      continue;
    }
    const startedMs = timestampToMs(item.timestamp);
    if (startedMs === null || endedMs === null || endedMs < startedMs) {
      continue;
    }
    item.durationEstimateMs = Math.max(0, endedMs - startedMs);
  }
}

function timestampToMs(timestamp) {
  if (!timestamp) {
    return null;
  }
  const value = new Date(timestamp).getTime();
  return Number.isNaN(value) ? null : value;
}

function appendTextSection(
  assistantRun,
  {
    type,
    content,
    durationMs = null,
    event = null,
    message = null,
    streaming,
    interrupted = false,
  },
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
    if (durationMs !== null) {
      // The stable boundary replaces the streamed draft wholesale, so its
      // measured duration replaces any live-ticking estimate too.
      existingItem.durationMs = durationMs;
    }
    existingItem.durationEstimateMs = null;
    existingItem.streaming = streaming;
    existingItem.interrupted = interrupted;
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
    durationMs,
    durationEstimateMs: null,
    sequence,
    timestamp: event?.timestamp ?? message?.timestamp,
    streaming,
    interrupted,
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
  if (!isFinalizableTextDraft(lastMatchingItem)) {
    const previousMessage =
      lastMatchingItem.messages?.at(-1) ??
      lastMatchingItem.events?.at(-1)?.payload?.message;
    if (
      streaming ||
      (message?.id && previousMessage?.id && message.id !== previousMessage.id)
    )
      return null;
  }

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
  if (isPlainObject(payload.preview_arguments)) {
    tool.previewArguments = payload.preview_arguments;
  }
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
  tool.previewArguments = null;
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
  tool.display =
    event.payload?.display ??
    event.payload?.message?.tool_display ??
    tool.display;
  tool.resultEvent = event;
  tool.timing =
    normalizedTiming(event.payload?.timing ?? event.payload?.message?.timing) ??
    tool.timing;
  tool.durationMs = timingDurationMs(tool.timing) ?? tool.durationMs;
  tool.status = toolStatusFromResultEvent(event);
  tool.events = [...tool.events, event];
  syncAssistantRunCollections(assistantRun);
}

// A per-tool-call user cancel returns the stable `cancelled_by_user` failure
// envelope; the row renders as "cancelled" — the user's own action — instead
// of a red failure. Any other failure envelope stays "failed".
function toolStatusFromResultEvent(event) {
  const result = event.payload?.result ?? event.payload?.message?.content;
  if (toolResultCancelledByUser(result)) {
    return CHAT_STATUS_CANCELLED;
  }
  if (hasToolResultFailure(event)) {
    return CHAT_STATUS_FAILED;
  }
  return hasResultPartial(result) ? CHAT_STATUS_PARTIAL : 'success';
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
    previewArguments: null,
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
      item.status === CHAT_STATUS_INTERRUPTED ||
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
    'model_step_usage',
    RUN_EVENT_CHANGE_STATS,
    'model_fallback_activated',
    RUN_EVENT_PROVIDER_HEARTBEAT,
    RUN_EVENT_REASONING_DELTA,
    'reasoning',
    RUN_EVENT_TOOL_CALL_DELTA,
    'tool_call_started',
    RUN_EVENT_TOOL_CALL_STDOUT,
    RUN_EVENT_TOOL_CALL_STDERR,
    'tool_call_result',
    'subagent_session_started',
    'subagent_status_changed',
    'compaction_started',
    'compaction_aborted',
    'compaction_completed',
    RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
    'assistant_output',
    'run_completed',
    'run_failed',
    'run_cancelled',
    'run_interrupted',
  ].includes(event?.type);
}

function normalizedIterationCount(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
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

// Failure code the per-tool-call user cancel produces (the bash tool's
// `tool_failure("cancelled_by_user", …)` envelope).
const USER_CANCELLED_TOOL_RESULT_CODE = 'cancelled_by_user';

function toolResultCancelledByUser(result) {
  const normalizedResult = parseResult(result);
  return normalizedResult?.error?.code === USER_CANCELLED_TOOL_RESULT_CODE;
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

function hasResultPartial(result) {
  const normalizedResult = parseResult(result);
  return (
    normalizedResult?.ok === true &&
    normalizedResult?.data?.status === 'partial'
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

function stripTimelineSequence({ sequence: _sequence, ...item }) {
  return item;
}
