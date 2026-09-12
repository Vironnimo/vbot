import {
  timestampToMs,
  syncAssistantRunCollections,
  firstSeenSequence,
  toolKeyFromToolCall,
  toolKeyFromValues,
  CHAT_STATUS_RUNNING,
  normalizedTiming,
  timingDurationMs,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_PARTIAL,
  CHAT_STATUS_CANCELLED,
  toolMatchesCall,
  moreStableToolKey,
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_INTERRUPTED,
} from './model.js';
import { isPlainObject } from '../values.js';
import { RUN_EVENT_TOOL_CALL_STDERR } from '../api.js';

// Reasoning has no explicit end event: any other live Run event (except the
// idle heartbeat) means the streamed reasoning draft stopped growing, so its
// ticking header freezes at that boundary instead of counting on through the
// following Tool Calls. The draft itself stays open for merging; the next
// stable boundary replaces the frozen estimate with the measured server-side
// span via appendTextSection.
export function freezeStreamingReasoningEstimates(
  assistantRun,
  endedTimestamp,
) {
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

export function appendTextSection(
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

export function appendToolDelta(assistantRun, event) {
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

export function mergeToolStarted(assistantRun, event) {
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

export function mergeToolOutput(assistantRun, event) {
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

export function mergeToolResult(assistantRun, event) {
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

export function mergeSubAgentSessionStarted(assistantRun, event) {
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

export function markPendingToolsCancelled(assistantRun, event) {
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

// Failure code the per-tool-call user cancel produces (the bash tool's
// `tool_failure("cancelled_by_user", …)` envelope).
const USER_CANCELLED_TOOL_RESULT_CODE = 'cancelled_by_user';

export function toolResultCancelledByUser(result) {
  const normalizedResult = parseResult(result);
  return normalizedResult?.error?.code === USER_CANCELLED_TOOL_RESULT_CODE;
}

function hasToolResultFailure(event) {
  return (
    Boolean(event.payload?.error) || hasResultFailure(event.payload?.result)
  );
}

export function hasResultFailure(result) {
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
