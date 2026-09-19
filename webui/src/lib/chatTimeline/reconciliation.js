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

// Completeness is an explicit database read fact from the same History snapshot.
export function runProjectionPersistedInHistory(runs, runId) {
  return Boolean(runId && runs?.[runId]?.complete);
}

export function pruneRunEventsPersistedInHistory(runEvents, runs, activeRunId) {
  const retired = new Set();
  const checked = new Set();
  for (const event of runEvents ?? []) {
    if (checked.has(event.run_id)) continue;
    checked.add(event.run_id);
    if (
      event.run_id !== activeRunId &&
      !retired.has(event.run_id) &&
      runProjectionPersistedInHistory(runs, event.run_id)
    ) {
      retired.add(event.run_id);
    }
  }
  return retired.size
    ? (runEvents ?? []).filter((event) => !retired.has(event.run_id))
    : (runEvents ?? []);
}

export function reconcileTimeline(sessionState, liveItems) {
  const messages = sessionState.messages ?? [];
  const retired = new Set(
    liveItems
      .filter(
        (item) =>
          item.type === 'assistant_run' &&
          (item.status !== 'running' ||
            sessionState.currentRun?.runId !== item.runId) &&
          runProjectionPersistedInHistory(sessionState.historyRuns, item.runId),
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

  const owner = (message) => message.history_run_id;
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
      phaseMessageId =
        child.assistantMessageId ?? toolMessages.get(child.toolCallId) ?? null;
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
    return (
      saved.toolCallId &&
      saved.toolCallId === live.toolCallId &&
      (!saved.assistantMessageId ||
        !live.assistantMessageId ||
        saved.assistantMessageId === live.assistantMessageId)
    );
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
