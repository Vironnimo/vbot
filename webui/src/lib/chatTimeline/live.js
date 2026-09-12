import {
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
  RUN_EVENT_CHANGE_STATS,
  RUN_EVENT_PROVIDER_HEARTBEAT,
  RUN_EVENT_TOOL_CALL_STDERR,
  RUN_EVENT_TOOL_CALL_STDOUT,
} from '../api.js';
import {
  historyMessageItem,
  TERMINAL_RUN_EVENTS,
  isStreamingDeltaEvent,
  createAssistantRunItem,
  syncAssistantRunCollections,
  CHAT_STATUS_RUNNING,
  CHAT_STATUS_COMPLETED,
  terminalStatus,
  normalizedIterationCount,
  normalizedTiming,
  timingDurationMs,
} from './model.js';
import { isPlainObject } from '../values.js';
import {
  freezeStreamingReasoningEstimates,
  appendTextSection,
  appendToolDelta,
  mergeToolStarted,
  mergeToolOutput,
  mergeToolResult,
  mergeSubAgentSessionStarted,
  markPendingToolsCancelled,
} from './runChildren.js';

const PROVIDER_PROGRESS_RUN_EVENTS = new Set([
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
  'reasoning',
  'assistant_output',
  'tool_call_started',
]);

export function liveTimelineItems(runEvents, projectionCache = null) {
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

export function appendLiveRunEvent(assistantRun, event) {
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

function shouldShowStandaloneRunEvent(event) {
  return event?.type === 'user_message_persisted';
}

function compareRunEvents(left, right) {
  return (left.sequence ?? 0) - (right.sequence ?? 0);
}

function textFromRunEventMessage(event, key) {
  const message = event.payload?.message;
  if (message?.[key]) {
    return message[key];
  }
  return event.payload?.[key] ?? '';
}
