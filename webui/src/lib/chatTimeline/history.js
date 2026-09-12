import {
  createAssistantRunItem,
  historyMessageItem,
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
  mergeToolStarted,
  mergeToolResult,
  toolResultCancelledByUser,
  hasResultFailure,
  markPendingToolsCancelled,
} from './runChildren.js';
import { isPlainObject } from '../values.js';

export function historyTimelineItems(messages) {
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

function pushActiveAssistantRun(timelineItems, assistantRun) {
  if (!assistantRun) {
    return;
  }
  syncAssistantRunCollections(assistantRun);
  timelineItems.push(stripTimelineSequence(assistantRun));
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
