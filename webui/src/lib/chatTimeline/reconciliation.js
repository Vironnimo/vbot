import {
  mergeTimelineItems,
  createAssistantRunItem,
  syncAssistantRunCollections,
  RUN_HISTORY_CONTENT_ROLES,
} from './model.js';
import {
  historyTimelineItems,
  appendHistoryAssistantMessage,
  appendHistoryToolResult,
} from './history.js';
import { appendLiveRunEvent } from './live.js';
import { markPendingToolsCancelled } from './runChildren.js';

// Only a canonical terminal record proves that the complete Run is in History.
// Stable output IDs identify individual Messages, never completeness of a Run.
export function runProjectionPersistedInHistory(
  runEvents,
  messages,
  runId,
  generation = '',
) {
  if (!runId) return false;
  if (
    (messages ?? []).some(
      (message) => message.role === 'run_summary' && message.run_id === runId,
    )
  )
    return true;
  const checkpoint = (runEvents ?? []).find(
    (event) => event.run_id === runId && event.type === 'run_completed',
  )?.payload?.history_checkpoint;
  return Boolean(
    checkpoint &&
    generation === checkpoint.generation_id &&
    (messages ?? []).some(
      (message) =>
        message.role === 'compaction_checkpoint' &&
        message.history_sequence === checkpoint.sequence,
    ),
  );
}

export function pruneRunEventsPersistedInHistory(
  runEvents,
  messages,
  activeRunId,
  generation = '',
) {
  const retired = new Set();
  const checked = new Set();
  for (const event of runEvents ?? []) {
    if (checked.has(event.run_id)) continue;
    checked.add(event.run_id);
    if (
      event.run_id !== activeRunId &&
      !retired.has(event.run_id) &&
      runProjectionPersistedInHistory(
        runEvents,
        messages,
        event.run_id,
        generation,
      )
    ) {
      retired.add(event.run_id);
    }
  }
  return retired.size
    ? (runEvents ?? []).filter((event) => !retired.has(event.run_id))
    : (runEvents ?? []);
}

export function reconcileTimeline(
  sessionState,
  liveItems,
  runEvents = sessionState.runEvents,
) {
  const messages = sessionState.messages ?? [];
  const retired = new Set(
    liveItems
      .filter(
        (item) =>
          item.type === 'assistant_run' &&
          (item.status !== 'running' ||
            sessionState.currentRun?.runId !== item.runId) &&
          runProjectionPersistedInHistory(
            runEvents,
            messages,
            item.runId,
            sessionState.historyGeneration,
          ),
      )
      .map((item) => item.runId),
  );
  const remainingLive = liveItems.filter(
    (item) => !retired.has(itemRunId(item)),
  );
  const runs = new Map(
    remainingLive
      .filter((item) => item.type === 'assistant_run')
      .map((item) => [item.runId, item]),
  );

  // A stable event also carries an exact Message reference. This joins errors
  // and checkpoints without guessing from text, timestamps, or adjacent roles.
  const eventOwners = new Map(
    (runEvents ?? [])
      .filter((event) => event.payload?.message?.id)
      .map((event) => [event.payload.message.id, event.run_id]),
  );
  const owner = (message) =>
    Object.hasOwn(message, 'history_run_id')
      ? message.history_run_id
      : eventOwners.get(message.id);
  const owned = new Map();
  for (const message of messages) {
    const runId = owner(message);
    if (!runs.has(runId)) continue;
    if (!owned.has(runId)) owned.set(runId, []);
    owned.get(runId).push(message);
  }
  const mergedRuns = new Map();
  for (const [runId, rows] of owned) {
    const liveRun = runs.get(runId);
    mergedRuns.set(runId, mergeRun(rows, liveRun));
  }

  // Preserve canonical record order. A live overlay replaces precisely its
  // server-attributed segment; it never consumes another Run's adjacent output.
  const timeline = [];
  const emitted = new Set();
  let history = [];
  const flushHistory = () => {
    timeline.push(...historyTimelineItems(history));
    history = [];
  };
  for (const message of messages) {
    const runId = owner(message);
    const merged = mergedRuns.get(runId);
    if (!merged) {
      history.push(message);
      continue;
    }
    if (message.role === 'user') {
      history.push(message);
      continue;
    }
    flushHistory();
    if (!emitted.has(runId)) {
      timeline.push(merged);
      emitted.add(runId);
    }
    if (
      !RUN_HISTORY_CONTENT_ROLES.has(message.role) &&
      message.role !== 'run_summary'
    ) {
      timeline.push(...historyTimelineItems([message]));
    }
  }
  flushHistory();
  for (const [runId, merged] of mergedRuns) {
    if (emitted.has(runId)) continue;
    const user = owned
      .get(runId)
      .findLast((message) => message.role === 'user');
    const index = user
      ? timeline.findIndex((item) => item.message === user)
      : -1;
    timeline.splice(index < 0 ? timeline.length : index + 1, 0, merged);
    emitted.add(runId);
  }
  const messageIds = new Set(messages.map((message) => message.id));
  return mergeTimelineItems(
    timeline,
    remainingLive.filter((item) => {
      if (item.type === 'assistant_run') return !emitted.has(item.runId);
      const message = item.message ?? item.event?.payload?.message;
      return !message?.id || !messageIds.has(message.id);
    }),
  );
}

function itemRunId(item) {
  return item.runId ?? item.liveErrorRunId ?? item.event?.run_id;
}

function mergeRun(messages, liveRun) {
  const historyRun = createAssistantRunItem({
    id: liveRun.id,
    runId: liveRun.runId,
    source: 'history',
  });
  for (const message of messages) {
    if (message.role === 'assistant')
      appendHistoryAssistantMessage(historyRun, message);
    else if (message.role === 'tool')
      appendHistoryToolResult(historyRun, message);
    else if (message.role === 'compaction_checkpoint')
      appendLiveRunEvent(historyRun, {
        type: 'compaction_completed',
        sequence: historyRun.items.length,
        timestamp: message.timestamp,
        payload: { message },
      });
  }
  const items = [...historyRun.items];
  const toolMessages = new Map(
    messages
      .filter((message) => message.role === 'assistant')
      .flatMap((message) =>
        (message.tool_calls ?? []).map((call) => [call.id, message.id]),
      ),
  );
  let insertionIndex = items.length;
  let phaseMessageId = null;
  for (const child of [...liveRun.items].reverse()) {
    // A dispatched Tool identifies the completed Assistant response immediately
    // before it. Its preceding text draft belongs to that exact Message even
    // when a sparse replay omitted the stable Assistant event.
    if (child.type === 'tool_call')
      phaseMessageId = toolMessages.get(child.toolCallId) ?? null;
    else if (child.type === 'compaction_separator') phaseMessageId = null;
    else
      phaseMessageId =
        (child.events ?? []).findLast((event) => event.payload?.message?.id)
          ?.payload.message.id ?? phaseMessageId;
    const index = items.findIndex((saved) =>
      childrenMatch(saved, child, phaseMessageId),
    );
    if (index < 0) items.splice(insertionIndex, 0, child);
    else {
      const saved = items[index];
      items[index] =
        child.streaming && phaseMessageId && child.type !== 'tool_call'
          ? saved
          : saved.type === 'tool_call' &&
              saved.resultEvent &&
              !child.resultEvent
            ? {
                ...saved,
                stdout: child.stdout || saved.stdout,
                stderr: child.stderr || saved.stderr,
              }
            : { ...saved, ...child };
      insertionIndex = index;
    }
  }
  const result = {
    ...liveRun,
    timestamp: messages[0]?.timestamp ?? liveRun.timestamp,
    items: items.map((item, sequence) => ({ ...item, sequence })),
  };
  if (liveRun.terminalEvent?.type === 'run_cancelled') {
    markPendingToolsCancelled(result, liveRun.terminalEvent);
  }
  syncAssistantRunCollections(result);
  return result;
}

function childrenMatch(saved, live, phaseMessageId) {
  if (saved.type !== live.type) return false;
  if (saved.type === 'tool_call')
    return saved.toolCallId && saved.toolCallId === live.toolCallId;
  if (saved.type === 'compaction_separator')
    return saved.message?.id && saved.message.id === live.message?.id;
  const ids = new Set(
    (saved.messages ?? []).map((message) => message.id).filter(Boolean),
  );
  if (phaseMessageId && ids.has(phaseMessageId)) return true;
  return (live.events ?? []).some((event) =>
    ids.has(event.payload?.message?.id),
  );
}
