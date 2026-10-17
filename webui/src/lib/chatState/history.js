import { loadTimelineItems } from '../chatTimeline.js';
import {
  CHAT_STATUS_IDLE,
  isRecord,
  isRunActive,
  nextContextUsage,
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
  const retainedRunEvents = changedGeneration
    ? []
    : sessionState.runEvents.filter(
        (event) =>
          (!options.reset || event.run_id === options.activeRunId) &&
          (event.run_id === options.activeRunId ||
            !runs[event.run_id]?.complete),
      );
  const retainLiveRunProjection =
    !changedGeneration &&
    (isRunActive(sessionState) ||
      (sessionState.currentRun &&
        !runs[sessionState.currentRun.runId]?.complete &&
        sessionState.timelineItems.some(
          (item) => item.runId === sessionState.currentRun.runId,
        )));
  loadTimelineItems(sessionState, options.items, {
    ...options,
    incremental,
    reset: changedGeneration || options.reset,
  });
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
    sessionState.contextUsage = nextContextUsage(
      sessionState.contextUsage,
      options.contextUsage,
    );
  }
  if (isRecord(options.compactionPolicy)) {
    sessionState.compactionPolicy = options.compactionPolicy;
  }
  sessionState.backgroundCommandStatuses = nextBackgroundCommandStatuses(
    incremental ? sessionState.backgroundCommandStatuses : {},
    options,
  );
  // Complete in every response that carries it.
  if (Array.isArray(options.subAgentBackgroundCommands)) {
    sessionState.subAgentBackgroundCommands =
      options.subAgentBackgroundCommands.flatMap(subAgentBackgroundCommand);
  }
  return sessionState;
}

function subAgentBackgroundCommand(entry) {
  const text = (value) => (typeof value === 'string' ? value.trim() : '');
  const command = {
    subAgentId: text(entry?.subagent_id),
    sessionId: text(entry?.session_id),
    commandId: text(entry?.command_id),
    label: text(entry?.label),
  };
  return command.subAgentId && command.sessionId && command.commandId
    ? [command]
    : [];
}

// An incremental response reports the statuses of its appended records and
// of every live command of the Session (none when absent); any other
// response reports them all. A `running` it does not report has therefore
// ended, its end unknown, as after a server restart. The commands whose
// processes still run after their shell exited are listed in full whenever
// the response reports statuses.
function nextBackgroundCommandStatuses(known, options) {
  const entries = { ...known };
  if (isRecord(options.backgroundCommandStatuses)) {
    const statuses = options.backgroundCommandStatuses;
    for (const [commandId, entry] of Object.entries(entries)) {
      if (entry?.status === 'running' && !Object.hasOwn(statuses, commandId)) {
        entries[commandId] = { status: 'ended', stillRunning: false };
      }
    }
    for (const [commandId, status] of Object.entries(statuses)) {
      entries[commandId] = { status, stillRunning: false };
    }
  }
  if (Array.isArray(options.backgroundCommandsStillRunning)) {
    const stillRunning = new Set(options.backgroundCommandsStillRunning);
    for (const [commandId, entry] of Object.entries(entries)) {
      entries[commandId] = {
        ...entry,
        stillRunning: stillRunning.has(commandId),
      };
    }
  }
  return entries;
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
  if (
    !runId ||
    !TERMINAL_RUN_STATUSES.has(runStatus) ||
    sessionState.historyRuns?.[runId]?.complete === true
  ) {
    return false;
  }
  return (sessionState.timelineItems ?? []).some(
    (item) => item.runId === runId,
  );
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
  loadTimelineItems(sessionState, options.items, { ...options, prepend: true });
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
  const itemIndex = sessionState.timelineItems.findIndex(
    (item) =>
      item.message?.id === messageId ||
      item.items?.some((child) => child.message?.id === messageId),
  );
  if (itemIndex >= 0)
    sessionState.timelineItems = sessionState.timelineItems.slice(0, itemIndex);
  sessionState.timelineSequences = {};
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
    'model_fallback',
    'goal_event',
  ].includes(message?.role);
}
