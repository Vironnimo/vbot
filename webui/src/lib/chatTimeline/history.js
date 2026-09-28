import {
  createAssistantRunItem,
  historyMessageItem,
  historyMessageKey,
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_FAILED,
  syncAssistantRunCollections,
  stripTimelineSequence,
  CHAT_STATUS_CANCELLED,
  normalizedIterationCount,
  normalizedTiming,
  timingDurationMs,
} from './model.js';
import {
  appendTextSection,
  appendSteeringMessage,
  mergeToolStarted,
  mergeToolResult,
  toolResultCancelledByUser,
  hasResultFailure,
  markPendingToolsCancelled,
} from './runChildren.js';
import { isPlainObject } from '../values.js';

// A History item is a pure projection of the Messages it was built from.
// `reuse` (see `createHistoryItemReuse`) hands back the object an earlier
// projection built from the same Messages, so keyed rendering skips unchanged
// rows instead of re-rendering the whole History on every streaming flush.
export function historyTimelineItems(messages, reuse = null) {
  const timelineItems = [];
  const push = (item, sources) =>
    timelineItems.push(reuseHistoryItem(reuse, item, sources));
  let activeAssistantRun = null;
  let activeRunSources = [];
  let previousVisibleRole = '';
  let activeRecordRunId;
  const pushActiveRun = () => {
    pushActiveAssistantRun(push, activeAssistantRun, activeRunSources);
    activeAssistantRun = null;
    activeRunSources = [];
  };

  for (const message of messages ?? []) {
    // Missing terminal persistence must not merge two canonical executions.
    // Keep the existing within-Run presentation, including text-only rows.
    if (
      activeAssistantRun &&
      activeRecordRunId !== undefined &&
      Object.hasOwn(message, 'history_run_id') &&
      activeRecordRunId !== message.history_run_id
    ) {
      pushActiveRun();
      previousVisibleRole = '';
    }
    if (message?.role === 'compaction_checkpoint') {
      pushActiveRun();
      push(
        {
          id: `compaction-${historyMessageKey(message)}`,
          type: 'compaction_separator',
          timestamp: message.timestamp,
          message,
          durationMs: message?.usage?.compaction_duration_ms ?? null,
        },
        [message],
      );
      previousVisibleRole = 'compaction_checkpoint';
      continue;
    }

    if (message?.role === 'agent_takeover') {
      pushActiveRun();
      push(
        {
          id: `takeover-${historyMessageKey(message)}`,
          type: 'takeover_separator',
          timestamp: message.timestamp,
          message,
        },
        [message],
      );
      previousVisibleRole = 'agent_takeover';
      continue;
    }

    if (message?.role === 'run_summary') {
      if (activeAssistantRun) {
        appendHistoryRunSummary(activeAssistantRun, message);
        activeRunSources.push(message);
        pushActiveRun();
      } else if (
        message.status === 'cancelled' ||
        message.status === 'interrupted'
      ) {
        // A terminal run with no visible output has only its summary as a
        // durable trace. Render a bare status row instead of leaving a hole.
        push(terminalRunSummaryItem(message, timelineItems.length), [message]);
      }
      previousVisibleRole = 'run_summary';
      continue;
    }

    if (
      message?.role === 'user' &&
      activeAssistantRun &&
      message.history_run_id &&
      message.history_run_id === activeRecordRunId
    ) {
      appendSteeringMessage(activeAssistantRun, message);
      activeRunSources.push(message);
      previousVisibleRole = 'user';
      continue;
    }
    if (message?.role === 'user') {
      pushActiveRun();
      push(historyMessageItem(message), [message]);
      previousVisibleRole = 'user';
      continue;
    }

    if (message?.role === 'assistant') {
      const followsAssistant = previousVisibleRole === 'assistant';
      if (followsAssistant) {
        pushActiveRun();
      }

      if (
        !activeAssistantRun &&
        (hasToolCalls(message) ||
          previousTimelineItemIsUser(timelineItems) ||
          followsAssistant)
      ) {
        activeRecordRunId = message.history_run_id;
        activeAssistantRun = createAssistantRunItem({
          id: `history-run-${historyMessageKey(message)}`,
          runId: message.history_run_id ?? null,
          source: 'history',
          sequence: timelineItems.length,
          timestamp: message.timestamp,
        });
      }

      if (activeAssistantRun) {
        appendHistoryAssistantMessage(activeAssistantRun, message);
        activeRunSources.push(message);
        previousVisibleRole = 'assistant';
        continue;
      }

      push(historyMessageItem(message), [message]);
      previousVisibleRole = 'assistant';
      continue;
    }

    if (message?.role === 'tool' && activeAssistantRun) {
      appendHistoryToolResult(activeAssistantRun, message);
      activeRunSources.push(message);
      previousVisibleRole = 'tool';
      continue;
    }

    pushActiveRun();
    push(historyMessageItem(message), [message]);
    previousVisibleRole = message?.role ?? '';
  }

  pushActiveRun();
  return timelineItems;
}

// One projection generation per displayed Session: an item built from the
// same Messages as in the previous generation is handed back unchanged.
export function createHistoryItemReuse() {
  return { previous: new Map(), next: new Map() };
}

export function finishHistoryItemReuse(reuse) {
  reuse.previous = reuse.next;
  reuse.next = new Map();
}

function reuseHistoryItem(reuse, item, sources) {
  if (!reuse) {
    return item;
  }
  const cached = reuse.previous.get(item.id);
  const kept =
    cached && sameMessages(cached.sources, sources) ? cached.item : item;
  reuse.next.set(item.id, { sources, item: kept });
  return kept;
}

function sameMessages(left, right) {
  return (
    left.length === right.length &&
    left.every((message, index) => message === right[index])
  );
}

export function appendHistoryAssistantMessage(assistantRun, message) {
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
        assistant_message_id: message.id,
        tool_call: {
          index,
          ...toolCall,
        },
      },
    });
  }

  assistantRun.status = CHAT_STATUS_COMPLETED;
}

export function appendHistoryToolResult(assistantRun, message) {
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
    id: `history-run-summary-${historyMessageKey(message)}`,
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

function pushActiveAssistantRun(push, assistantRun, sources) {
  if (!assistantRun) {
    return;
  }
  syncAssistantRunCollections(assistantRun);
  push(stripTimelineSequence(assistantRun), sources);
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
