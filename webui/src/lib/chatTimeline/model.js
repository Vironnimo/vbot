import {
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
} from '../api.js';
import { isPlainObject } from '../values.js';

export const CHAT_STATUS_RUNNING = 'running';

export const CHAT_STATUS_COMPLETED = 'completed';

export const CHAT_STATUS_FAILED = 'failed';

export const CHAT_STATUS_PARTIAL = 'partial';

export const CHAT_STATUS_CANCELLED = 'cancelled';

export const CHAT_STATUS_INTERRUPTED = 'interrupted';

export const RUN_HISTORY_CONTENT_ROLES = new Set([
  'assistant',
  'tool',
  'compaction_checkpoint',
]);

export const TERMINAL_RUN_EVENTS = new Set([
  'run_completed',
  'run_failed',
  'run_cancelled',
  'run_interrupted',
]);

export function isRunActive(sessionState) {
  return sessionState?.status === CHAT_STATUS_RUNNING;
}

// Insert retained Run groups into canonical History without sorting History
// itself or separating a live User message from its Assistant output.
export function mergeTimelineItems(historyItems, liveItems) {
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

export function terminalStatus(eventType) {
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

export function isStreamingDeltaEvent(eventType) {
  return [
    RUN_EVENT_REASONING_DELTA,
    RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
    RUN_EVENT_TOOL_CALL_DELTA,
  ].includes(eventType);
}

export function createAssistantRunItem({
  id,
  runId,
  source,
  sequence,
  timestamp,
}) {
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

export function timestampToMs(timestamp) {
  if (!timestamp) {
    return null;
  }
  const value = new Date(timestamp).getTime();
  return Number.isNaN(value) ? null : value;
}

export function syncAssistantRunCollections(assistantRun) {
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

export function historyMessageItem(message) {
  return {
    id: message.id ?? `history-${message.role}-${message.timestamp}`,
    type: 'message',
    message,
  };
}

export function normalizedIterationCount(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function compareTimelineChildren(left, right) {
  return (left.sequence ?? 0) - (right.sequence ?? 0);
}

export function firstSeenSequence(existingSequence, candidateSequence) {
  if (!Number.isFinite(existingSequence)) {
    return candidateSequence;
  }
  if (!Number.isFinite(candidateSequence)) {
    return existingSequence;
  }
  return Math.min(existingSequence, candidateSequence);
}

export function toolKeyFromToolCall(toolCall) {
  return toolKeyFromValues(toolCall?.id, toolCall?.index);
}

export function toolKeyFromValues(id, index) {
  if (id !== undefined && id !== null && id !== '') {
    return `id-${id}`;
  }
  if (index !== undefined && index !== null) {
    return `index-${index}`;
  }
  return 'unknown';
}

export function toolMatchesCall(tool, toolCall) {
  if (toolCall?.id && tool.toolCallId === toolCall.id) {
    return true;
  }
  return (
    !tool.toolCallId &&
    toolCall?.index !== undefined &&
    tool.index === toolCall.index
  );
}

export function moreStableToolKey(existingKey, candidateKey) {
  if (candidateKey?.startsWith('id-')) {
    return candidateKey;
  }
  return existingKey;
}

export function normalizedTiming(timing) {
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

export function timingDurationMs(timing) {
  return Number.isFinite(timing?.duration_ms) && timing.duration_ms >= 0
    ? timing.duration_ms
    : null;
}

export function stripTimelineSequence({ sequence: _sequence, ...item }) {
  return item;
}
