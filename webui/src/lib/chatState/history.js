import {
  pruneRunEventsPersistedInHistory,
  runProjectionPersistedInHistory,
} from '../chatTimeline.js';
import {
  CHAT_STATUS_IDLE,
  isRecord,
  isRunActive,
  TERMINAL_RUN_STATUSES,
} from './sessionState.js';

export function loadHistory(sessionState, messages, options = {}) {
  const incomingMessages = Array.isArray(messages)
    ? messages.filter(isVisibleHistoryMessage)
    : [];
  const changedGeneration = Boolean(
    sessionState.historyGeneration &&
    options.generation &&
    options.generation !== sessionState.historyGeneration,
  );
  const incremental = options.incremental === true && !changedGeneration;
  const visibleMessages = incremental
    ? mergeHistoryRecords(sessionState.messages, incomingMessages)
    : incomingMessages;
  const runs = incremental ? { ...sessionState.historyRuns } : {};
  for (const run of options.runs ?? []) runs[run.run_id] = run;
  sessionState.historyRuns = runs;
  const activeRunId = isRunActive(sessionState)
    ? sessionState.currentRun?.runId
    : null;
  const keepGeneration = (event) =>
    !changedGeneration &&
    (!options.reset || event.run_id === options.activeRunId);
  const retainedRunEvents = changedGeneration
    ? []
    : pruneRunEventsPersistedInHistory(
        sessionState.runEvents.filter(keepGeneration),
        runs,
        activeRunId,
      );
  const retainedStreamingRunEvents = changedGeneration
    ? []
    : sessionState.streamingRunEvents
        .filter(keepGeneration)
        .filter(
          (event) =>
            event.run_id === activeRunId ||
            !runProjectionPersistedInHistory(runs, event.run_id),
        );
  const retainLiveRunProjection =
    !changedGeneration &&
    (isRunActive(sessionState) ||
      retainedRunEvents.length > 0 ||
      retainedStreamingRunEvents.length > 0);
  const retainedStreamingPhase = retainLiveRunProjection
    ? sessionState.streamingPhase
    : 0;
  const retainedRunIds = new Set([
    activeRunId,
    ...retainedRunEvents.map((event) => event.run_id),
    ...retainedStreamingRunEvents.map((event) => event.run_id),
  ]);
  const retainedSeenStreamingEventKeys = retainLiveRunProjection
    ? new Set(
        [...sessionState.seenStreamingEventKeys].filter((key) =>
          retainedRunIds.has(key.split(':').slice(0, -2).join(':')),
        ),
      )
    : new Set();
  sessionState.messages = visibleMessages;
  if (
    changedGeneration ||
    (options.reset && sessionState.currentRun?.runId !== options.activeRunId)
  ) {
    sessionState.currentRun = null;
    sessionState.status = CHAT_STATUS_IDLE;
  }
  sessionState.historyGeneration =
    options.generation ?? sessionState.historyGeneration;
  sessionState.historyAfter = options.nextAfter ?? '';
  sessionState.historySnapshotVersion =
    (sessionState.historySnapshotVersion ?? 0) + 1;
  sessionState.historyLoaded = true;
  if (!incremental) {
    sessionState.hasOlderHistory = options.hasMore === true;
    sessionState.historyBefore =
      options.hasMore === true && typeof options.nextBefore === 'string'
        ? options.nextBefore
        : '';
  }
  sessionState.runEvents = retainedRunEvents;
  sessionState.streamingRunEvents = retainedStreamingRunEvents;
  sessionState.streamingPhase = retainedStreamingPhase;
  sessionState.seenStreamingEventKeys = retainedSeenStreamingEventKeys;
  sessionState.error = null;
  if (!retainLiveRunProjection) {
    sessionState.status = CHAT_STATUS_IDLE;
    sessionState.streamError = '';
  }
  const lastUsage = findLastUsage(sessionState.messages);
  if (lastUsage) {
    sessionState.usage = lastUsage;
  }
  // Whole-session totals come from the server (the loaded page may be a
  // slice); terminal run events refresh them between history loads.
  if (options.sessionUsage) {
    sessionState.sessionUsage = options.sessionUsage;
  }
  if (Object.hasOwn(options, 'contextUsage')) {
    sessionState.contextUsage = options.contextUsage ?? null;
  }
  if (isRecord(options.compactionPolicy)) {
    sessionState.compactionPolicy = options.compactionPolicy;
  }
  sessionState.backgroundBashStatuses = isRecord(options.backgroundBashStatuses)
    ? { ...options.backgroundBashStatuses }
    : {};
  return sessionState;
}

function mergeHistoryRecords(existing, incoming) {
  const records = new Map(
    (existing ?? []).map((message) => [recordKey(message), message]),
  );
  for (const message of incoming) records.set(recordKey(message), message);
  return [...records.values()];
}

function recordKey(message) {
  return Number.isInteger(message.history_sequence)
    ? message.history_sequence
    : message.id;
}

export function hasRetainedTerminalRunProjection(sessionState) {
  const runId = sessionState?.currentRun?.runId;
  const runStatus = sessionState?.currentRun?.status ?? sessionState?.status;
  if (!runId || !TERMINAL_RUN_STATUSES.has(runStatus)) {
    return false;
  }
  return [
    ...(sessionState.runEvents ?? []),
    ...(sessionState.streamingRunEvents ?? []),
  ].some((event) => event?.run_id === runId);
}

export function attachableHistoryRun(sessionState, activeRun) {
  if (!activeRun?.run_id) {
    return null;
  }
  const currentRun = sessionState?.currentRun;
  if (
    currentRun?.runId === activeRun.run_id &&
    TERMINAL_RUN_STATUSES.has(currentRun.status)
  ) {
    return null;
  }
  return activeRun;
}

export function prependHistory(sessionState, messages, options = {}) {
  for (const run of options.runs ?? [])
    sessionState.historyRuns[run.run_id] = run;
  const existingIds = new Set((sessionState.messages ?? []).map(recordKey));
  const olderMessages = Array.isArray(messages)
    ? messages
        .filter(isVisibleHistoryMessage)
        .filter((message) => !existingIds.has(recordKey(message)))
    : [];

  sessionState.messages = [...olderMessages, ...(sessionState.messages ?? [])];
  sessionState.hasOlderHistory = options.hasMore === true;
  sessionState.historyBefore =
    options.hasMore === true && typeof options.nextBefore === 'string'
      ? options.nextBefore
      : '';
  if (isRecord(options.backgroundBashStatuses)) {
    sessionState.backgroundBashStatuses = {
      ...options.backgroundBashStatuses,
    };
  }
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

export function truncateSessionForEdit(sessionState, messageId, acceptedRunId) {
  const targetIndex = (sessionState?.messages ?? []).findIndex(
    (message) => message?.id === messageId,
  );
  if (targetIndex < 0) {
    return false;
  }
  sessionState.messages = sessionState.messages.slice(0, targetIndex);
  sessionState.historyAfter = '';
  sessionState.historySnapshotVersion =
    (sessionState.historySnapshotVersion ?? 0) + 1;
  sessionState.runEvents = sessionState.runEvents.filter(
    (event) => acceptedRunId && event.run_id === acceptedRunId,
  );
  if (!acceptedRunId || sessionState.currentRun?.runId !== acceptedRunId) {
    sessionState.streamingRunEvents = [];
    sessionState.streamingPhase = 0;
    sessionState.seenStreamingEventKeys = new Set();
  }
  sessionState.usage = findLastUsage(sessionState.messages);
  sessionState.contextUsage = null;
  return true;
}

function isVisibleHistoryMessage(message) {
  return [
    'user',
    'assistant',
    'tool',
    'error',
    'compaction_checkpoint',
    'agent_takeover',
    'run_summary',
  ].includes(message?.role);
}
