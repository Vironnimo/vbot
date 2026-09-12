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
  const visibleMessages = Array.isArray(messages)
    ? messages.filter(isVisibleHistoryMessage)
    : [];
  const retainLiveRunProjection = shouldRetainLiveRunProjection(
    sessionState,
    visibleMessages,
  );
  // While a Run is active, or its just-finished output is newer than the
  // arriving History snapshot, retained events survive the reload. Events of
  // other Runs whose output the fresh History now persists are dead weight:
  // render-time dedup drops them anyway, so prune them here (handoff3 B10).
  const retainedRunEvents = retainLiveRunProjection
    ? pruneRunEventsPersistedInHistory(
        sessionState.runEvents,
        visibleMessages,
        sessionState.currentRun?.runId ?? null,
      )
    : [];
  const retainedStreamingRunEvents = retainLiveRunProjection
    ? sessionState.streamingRunEvents
    : [];
  const retainedStreamingPhase = retainLiveRunProjection
    ? sessionState.streamingPhase
    : 0;
  const retainedSeenStreamingEventKeys = retainLiveRunProjection
    ? sessionState.seenStreamingEventKeys
    : new Set();
  sessionState.messages = visibleMessages;
  sessionState.historySnapshotVersion =
    (sessionState.historySnapshotVersion ?? 0) + 1;
  sessionState.historyLoaded = true;
  sessionState.hasOlderHistory = options.hasMore === true;
  sessionState.historyBefore =
    options.hasMore === true && typeof options.nextBefore === 'string'
      ? options.nextBefore
      : '';
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
  sessionState.backgroundBashStatuses = isRecord(options.backgroundBashStatuses)
    ? { ...options.backgroundBashStatuses }
    : {};
  return sessionState;
}

function shouldRetainLiveRunProjection(sessionState, messages) {
  if (isRunActive(sessionState)) {
    return true;
  }

  const runId = sessionState?.currentRun?.runId;
  return (
    hasRetainedTerminalRunProjection(sessionState) &&
    !runProjectionPersistedInHistory(sessionState.runEvents, messages, runId)
  );
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

export function truncateSessionForEdit(sessionState, messageId) {
  const targetIndex = (sessionState?.messages ?? []).findIndex(
    (message) => message?.id === messageId,
  );
  if (targetIndex < 0) {
    return false;
  }
  sessionState.messages = sessionState.messages.slice(0, targetIndex);
  sessionState.historySnapshotVersion =
    (sessionState.historySnapshotVersion ?? 0) + 1;
  sessionState.runEvents = [];
  sessionState.streamingRunEvents = [];
  sessionState.streamingPhase = 0;
  sessionState.seenStreamingEventKeys = new Set();
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
