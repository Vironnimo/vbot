import { getAttachmentUrl } from '$lib/api.js';
import {
  compactToolValue,
  toolNameHasHiddenArguments,
} from '$lib/chatToolDetails.js';
import { t } from '$lib/i18n.js';

const TOOL_DISPLAY_ARGS = {
  read: ['path'],
  write: ['path'],
  edit: ['path'],
  bash: ['command'],
  glob: ['pattern'],
  grep: ['pattern', 'path'],
  subagent: ['agent_id', 'content'],
  subagent_result: ['agent_id', 'session_id'],
  web_fetch: ['url'],
  web_search: ['query'],
  process: ['action', 'session_id'],
  cron: ['action', 'id', 'agent_id', 'schedule_type'],
  channel_send: ['channel_id', 'message'],
  skill: ['name'],
};
const TOOL_NO_SUMMARY_NAMES = new Set(['status']);
const MAX_TOOL_LABEL_LENGTH = 80;
const MAX_SUBAGENT_PREVIEW_LENGTH = 96;
const SUBAGENT_TOOL_NAMES = new Set(['subagent', 'subagent_result']);

export { compactToolValue };

export const isUserItem = (item) =>
  item.type === 'streaming' || item.type === 'assistant_run'
    ? false
    : item.type === 'message'
      ? item.message.role === 'user'
      : item.event.type === 'user_message_persisted';

export const isAssistantItem = (item) =>
  item.type === 'assistant_run'
    ? true
    : item.type === 'streaming'
      ? ['assistant', 'reasoning', 'tool_call'].includes(
          item.streamingItem.type,
        )
      : item.type === 'message'
        ? item.message.role === 'assistant'
        : [
            'assistant_output',
            'reasoning',
            'tool_call_started',
            'tool_call_result',
          ].includes(item.event.type);

export const shouldRenderMessage = (message) =>
  hasUserContentBlocks(message) ||
  Boolean(textFromMessage(message)) ||
  hasReadableReasoning(message);

export const labelForMessage = (message) => {
  if (message.role === 'user') {
    return t('chat.role.user', 'You').toUpperCase();
  }
  if (message.role === 'assistant') {
    return t('chat.role.assistant', 'Assistant').toUpperCase();
  }
  if (message.role === 'system') {
    return t('chat.role.system', 'System').toUpperCase();
  }
  if (message.role === 'tool') {
    return t('chat.event.toolResult', 'Tool result').toUpperCase();
  }
  if (message.role === 'error') {
    return t('chat.role.error', 'Error').toUpperCase();
  }
  return t('common.unknown', 'Unknown').toUpperCase();
};

export const labelForEvent = (event) => {
  if (event.type === 'reasoning') {
    return t('chat.event.thinking', 'Thinking').toUpperCase();
  }
  if (event.type === 'tool_call_started') {
    return t('chat.event.toolStarted', 'Tool started').toUpperCase();
  }
  if (event.type === 'tool_call_result') {
    return t('chat.event.toolResult', 'Tool result').toUpperCase();
  }
  if (event.type === 'assistant_output') {
    return t('chat.role.assistant', 'Assistant').toUpperCase();
  }
  if (event.type === 'run_completed') {
    return t('chat.event.completed', 'Run completed');
  }
  if (event.type === 'run_failed') {
    return t('chat.event.failed', 'Run failed');
  }
  if (event.type === 'run_cancelled') {
    return t('chat.event.cancelled', 'Run cancelled');
  }
  if (event.type === 'user_message_persisted') {
    return t('chat.role.user', 'You').toUpperCase();
  }
  return t('common.unknown', 'Unknown').toUpperCase();
};

export const textFromMessage = (message) => {
  if (message.reasoning && !message.content) {
    return message.reasoning;
  }
  if (typeof message.content === 'string') {
    return message.content;
  }
  return '';
};

export const userContentBlocks = (message) => {
  if (!Array.isArray(message?.content)) {
    return [];
  }
  return message.content.filter((block) => isRenderableUserContentBlock(block));
};

export const hasUserContentBlocks = (message) =>
  message?.role === 'user' && userContentBlocks(message).length > 0;

export const isTextContentBlock = (block) =>
  isPlainObject(block) &&
  block.type === 'text' &&
  typeof block.text === 'string' &&
  block.text.trim() !== '';

export const isMediaContentBlock = (block) =>
  isPlainObject(block) &&
  block.type === 'media' &&
  trimmedString(block.attachment_id) !== '';

export const isImageMediaContentBlock = (block) =>
  isMediaContentBlock(block) &&
  trimmedString(block.media_type).startsWith('image/');

export const isFileContentBlock = (block) =>
  isPlainObject(block) &&
  block.type === 'file' &&
  trimmedString(block.attachment_id) !== '';

export const attachmentUrlForBlock = (block) =>
  attachmentUrlForId(block?.attachment_id);

export const attachmentFilename = (block) =>
  trimmedString(block?.filename) ||
  t('chat.attachment.fileLabel', 'Attached file');

export const attachmentPreviewLabel = (block) =>
  trimmedString(block?.filename) ||
  t('chat.attachment.preview', 'Preview attachment');

export const hasReadableReasoning = (message) =>
  message.role === 'assistant' && Boolean(message.reasoning);

export const hasAssistantContent = (message) =>
  message.role === 'assistant' && Boolean(message.content);

export const isReasoningOnlyAssistantMessage = (message) =>
  message.role === 'assistant' &&
  Boolean(message.reasoning) &&
  !message.content;

export const messageFromEvent = (event) => event.payload?.message ?? null;

export const toolCallFromEvent = (event) => event.payload?.tool_call ?? null;

export const textFromEvent = (event) => {
  const message = messageFromEvent(event);
  if (message) {
    return textFromMessage(message);
  }
  if (event.payload?.error) {
    return event.payload.error;
  }
  return event.payload?.status ?? '';
};

export const toolNameForEvent = (event) => {
  const toolCall = toolCallFromEvent(event);
  const message = messageFromEvent(event);
  return toolCall?.name ?? message?.name ?? t('common.unknown', 'Unknown');
};

export const toolArgumentForEvent = (event) => {
  const toolCall = toolCallFromEvent(event);
  if (!toolCall) {
    return '';
  }
  const displaySummary = trimmedString(toolDisplayFromEvent(event)?.summary);
  if (displaySummary) {
    return displaySummary;
  }
  return humanReadableToolLabel(toolCall?.name ?? '', toolCall.arguments ?? {});
};

export const toolRowFromEvent = (event) => ({
  name: toolNameForEvent(event),
  toolCall: toolCallFromEvent(event),
  display: toolDisplayFromEvent(event),
  startedEvent: event,
});

export const visibleRunChildren = (assistantRun) =>
  (assistantRun.items ?? []).filter((child) => {
    if (child.type === 'tool_call') {
      return shouldRenderToolCall(child);
    }
    return Boolean(child.content);
  });

export const runMetaParts = (assistantRun) =>
  [
    labelForRunIterations(assistantRun),
    formatRunDuration(assistantRun) || runStatusLabel(assistantRun.status),
  ].filter(Boolean);

export const toolStatus = (tool) => {
  if (tool.status === 'failed') {
    return 'failed';
  }
  if (tool.status === 'cancelled') {
    return 'cancelled';
  }
  if (tool.status === 'success' || tool.status === 'completed') {
    return 'success';
  }
  return 'running';
};

export const toolStatusLabel = (tool) => {
  if (toolStatus(tool) === 'cancelled') {
    return t('chat.toolCancelled', 'cancelled');
  }
  if (toolStatus(tool) === 'running') {
    return '';
  }
  return formatDurationMs(toolDurationMs(tool), 'chat.toolDurationSeconds');
};

export const toolArguments = (tool) =>
  tool.arguments ?? tool.toolCall?.arguments;

export const toolArgumentSummary = (tool) => {
  const displaySummary = trimmedString(toolDisplay(tool)?.summary);
  if (displaySummary) {
    return displaySummary;
  }

  const argumentsValue = toolArguments(tool);
  if (argumentsValue === undefined || argumentsValue === null) {
    return '';
  }
  return humanReadableToolLabel(toolNameForRunTool(tool), argumentsValue);
};

export const isSubAgentTool = (tool) =>
  SUBAGENT_TOOL_NAMES.has(toolNameForRunTool(tool));

export const isStartingBlockingSubAgent = (tool) => {
  if (toolNameForRunTool(tool) !== 'subagent' || !tool.startedEvent) {
    return false;
  }
  return subAgentArguments(tool).blocking !== false;
};

export const subAgentSessionId = (tool) => {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  return trimmedString(data.session_id) || trimmedString(args.session_id);
};

export const subAgentAgentId = (tool) => {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  return (
    trimmedString(args.agent_id) ||
    trimmedString(data.agent_id) ||
    t('common.unknown', 'Unknown')
  );
};

export const subAgentPreview = (tool) => {
  const args = subAgentArguments(tool);
  const toolName = toolNameForRunTool(tool);
  if (toolName === 'subagent') {
    return truncateToolLabel(
      trimmedString(args.content),
      MAX_SUBAGENT_PREVIEW_LENGTH,
    );
  }
  return truncateToolLabel(
    trimmedString(args.session_id),
    MAX_SUBAGENT_PREVIEW_LENGTH,
  );
};

export const subAgentDotStatus = (
  tool,
  assistantRun = null,
  subAgentStatuses = {},
) => {
  const parentStatus = toolStatus(tool);
  if (['failed', 'cancelled'].includes(parentStatus)) {
    return parentStatus;
  }

  const fetchedStatus = matchingSubAgentResultStatus(tool, assistantRun);
  if (fetchedStatus) {
    return fetchedStatus;
  }

  const externalStatus = externalSubAgentStatus(tool, subAgentStatuses);
  if (externalStatus) {
    return externalStatus;
  }

  const childStatus = subAgentChildStatus(tool);
  if (['running', 'queued'].includes(childStatus)) {
    return 'running';
  }
  if (['failed', 'error'].includes(childStatus)) {
    return 'failed';
  }
  if (childStatus === 'cancelled') {
    return 'cancelled';
  }
  if (['completed', 'success'].includes(childStatus)) {
    return 'success';
  }

  return parentStatus;
};

export const subAgentNavigationTarget = (tool) => {
  const data = subAgentResultData(tool);
  const agentId = trimmedString(data.agent_id);
  const sessionId = subAgentSessionId(tool);
  if (!agentId || !sessionId) {
    return null;
  }
  return { agentId, sessionId };
};

export const toolResultValueForEvent = (event) =>
  event.payload?.result ??
  event.payload?.error ??
  messageFromEvent(event)?.content;

export const isTextToSpeechResult = (event) => {
  if (toolNameForEvent(event) !== 'text_to_speech') {
    return false;
  }
  const result = event.payload?.result;
  if (!isPlainObject(result) || result.ok !== true) {
    return false;
  }
  const artifact = result.data?.artifact;
  return (
    isPlainObject(artifact) &&
    artifact.kind === 'speech' &&
    typeof artifact.url === 'string'
  );
};

export const speechArtifactFromResult = (event) => {
  const result = event.payload?.result;
  if (!isPlainObject(result)) {
    return null;
  }
  const artifact = result.data?.artifact;
  if (
    !isPlainObject(artifact) ||
    artifact.kind !== 'speech' ||
    typeof artifact.url !== 'string'
  ) {
    return null;
  }
  return artifact;
};

export const isTextToSpeechTool = (tool) => {
  if (toolNameForRunTool(tool) !== 'text_to_speech') {
    return false;
  }
  const result = tool.result;
  if (!isPlainObject(result) || result.ok !== true) {
    return false;
  }
  const artifact = result.data?.artifact;
  return (
    isPlainObject(artifact) &&
    artifact.kind === 'speech' &&
    typeof artifact.url === 'string'
  );
};

export const speechArtifactFromTool = (tool) => {
  const result = tool.result;
  if (!isPlainObject(result)) {
    return null;
  }
  const artifact = result.data?.artifact;
  if (
    !isPlainObject(artifact) ||
    artifact.kind !== 'speech' ||
    typeof artifact.url !== 'string'
  ) {
    return null;
  }
  return artifact;
};

export const formatTime = (timestamp) => {
  if (!timestamp) {
    return '';
  }
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  return new Intl.DateTimeFormat(undefined, {
    hour: 'numeric',
    minute: '2-digit',
  }).format(date);
};

export const formatDate = (timestamp) => {
  const dateKey = dateKeyForTimestamp(timestamp);
  if (isTodayDateKey(dateKey)) {
    return t('chat.today', 'Today');
  }

  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return t('chat.today', 'Today');
  }
  return new Intl.DateTimeFormat(undefined, {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  }).format(date);
};

export function dateKeyForTimestamp(timestamp) {
  if (!timestamp) {
    return todayDateKey();
  }

  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return todayDateKey();
  }
  return dateKeyForDate(date);
}

export function timestampForItem(item) {
  if (item.type === 'message') {
    return item.message.timestamp;
  }
  if (item.type === 'assistant_run') {
    return item.timestamp ?? item.startTimestamp ?? item.endTimestamp;
  }
  if (item.type === 'streaming') {
    return item.streamingItem?.timestamp;
  }
  if (item.type === 'compaction_separator') {
    return item.timestamp;
  }
  return item.event?.timestamp;
}

export const avatarForItem = (item) => {
  if (isUserItem(item)) {
    return t('chat.role.userAvatar', 'Y');
  }
  if (isAssistantItem(item)) {
    return t('chat.role.assistantAvatar', 'A');
  }
  return t('chat.role.systemAvatar', 'S');
};

export const metaForEvent = (event) => {
  if (event.type === 'run_failed') {
    return t('chat.runStatus.failed', 'Failed');
  }
  if (event.type === 'run_cancelled') {
    return t('chat.runStatus.cancelled', 'Cancelled');
  }
  if (event.type === 'run_completed') {
    return t('chat.runStatus.completed', 'Completed');
  }
  return '';
};

export const isToolEvent = (event) =>
  event.type === 'tool_call_started' || event.type === 'tool_call_result';

export const isRunningToolEvent = (event) => event.type === 'tool_call_started';

export const isFailedToolEvent = (event) =>
  event.type === 'tool_call_result' && hasToolResultError(event);

export const isTerminalEvent = (event) => event.type.startsWith('run_');

export const labelForStreamingItem = (streamingItem) => {
  if (streamingItem.type === 'reasoning') {
    return t('chat.event.thinking', 'Thinking').toUpperCase();
  }
  if (streamingItem.type === 'tool_call') {
    return t('chat.event.toolPreparing', 'Preparing tool').toUpperCase();
  }
  return t('chat.role.assistant', 'Assistant').toUpperCase();
};

export const streamingToolName = (streamingItem) =>
  streamingItem.name || t('chat.toolPendingName', 'tool');

export const shouldRenderStreamingItem = (streamingItem, timelineItems) => {
  if (!['assistant', 'reasoning'].includes(streamingItem?.type)) {
    return true;
  }

  if (!streamingItem?.run_id) {
    return true;
  }

  return !timelineItems.some(
    (item) =>
      item.type === 'assistant_run' && item.runId === streamingItem.run_id,
  );
};

export const latestTerminalStateForItems = (timelineItems) => {
  for (let index = timelineItems.length - 1; index >= 0; index -= 1) {
    const item = timelineItems[index];
    if (item?.type === 'assistant_run') {
      const terminalType = item.terminalEvent?.type;
      if (typeof terminalType === 'string' && terminalType.startsWith('run_')) {
        return {
          itemId: item.id,
          failed: terminalType === 'run_failed',
        };
      }
      continue;
    }
    if (
      item?.type === 'event' &&
      typeof item.event?.type === 'string' &&
      item.event.type.startsWith('run_')
    ) {
      return {
        itemId: item.id,
        failed: item.event.type === 'run_failed',
      };
    }
  }
  return {
    itemId: '',
    failed: false,
  };
};

export const shouldRenderRetryButton = (item, latestTerminalState) =>
  latestTerminalState.failed &&
  item?.id === latestTerminalState.itemId &&
  ((item.type === 'assistant_run' &&
    item.terminalEvent?.type === 'run_failed') ||
    (item.type === 'event' && item.event?.type === 'run_failed'));

export const toolNameForRunTool = (tool) =>
  tool.name || tool.toolCall?.name || t('chat.toolPendingName', 'tool');

function isRenderableUserContentBlock(block) {
  return (
    isTextContentBlock(block) ||
    isMediaContentBlock(block) ||
    isFileContentBlock(block)
  );
}

function attachmentUrlForId(attachmentId) {
  const normalizedId = trimmedString(attachmentId);
  if (!normalizedId) {
    return '';
  }

  try {
    return getAttachmentUrl(normalizedId);
  } catch {
    return '';
  }
}

function shouldRenderToolCall(tool) {
  if (isSubAgentTool(tool)) {
    return Boolean(
      subAgentNavigationTarget(tool) ||
      tool.resultEvent ||
      isStartingBlockingSubAgent(tool),
    );
  }
  return Boolean(
    tool.startedEvent || tool.resultEvent || tool.stdout || tool.stderr,
  );
}

function runIterationCount(assistantRun) {
  const outputCount = (assistantRun.outputs ?? []).length;
  const toolCount = (assistantRun.tools ?? []).length;
  return Math.max(1, outputCount + (toolCount > 0 ? 1 : 0));
}

function labelForRunIterations(assistantRun) {
  return t('chat.runIterations', '{count} iter', {
    count: runIterationCount(assistantRun),
  });
}

function runStatusLabel(status) {
  if (status === 'failed') {
    return t('chat.runStatus.failed', 'Failed');
  }
  if (status === 'cancelled') {
    return t('chat.runStatus.cancelled', 'Cancelled');
  }
  if (status === 'completed' || status === 'success') {
    return t('chat.runStatus.completed', 'Completed');
  }
  return t('chat.runStatus.running', 'Running');
}

function formatRunDuration(assistantRun) {
  const durationFromTiming = formatDurationMs(
    assistantRun.durationMs,
    'chat.runDurationSeconds',
  );
  if (durationFromTiming) {
    return durationFromTiming;
  }
  const start = timestampToMs(
    assistantRun.startTimestamp ?? assistantRun.timestamp,
  );
  const end = timestampToMs(assistantRun.endTimestamp);
  if (start === null || end === null || end < start) {
    return '';
  }
  return formatDurationMs(end - start, 'chat.runDurationSeconds');
}

function formatDurationMs(durationMs, i18nKey = 'chat.runDurationSeconds') {
  if (!Number.isFinite(durationMs) || durationMs < 0) {
    return '';
  }
  const elapsedSeconds = durationMs / 1000;
  if (elapsedSeconds < 10) {
    return t(i18nKey, '{seconds}s', {
      seconds: elapsedSeconds.toFixed(1),
    });
  }
  return t(i18nKey, '{seconds}s', {
    seconds: Math.round(elapsedSeconds),
  });
}

function timestampToMs(timestamp) {
  if (!timestamp) {
    return null;
  }
  const value = new Date(timestamp).getTime();
  return Number.isNaN(value) ? null : value;
}

function toolDurationMs(tool) {
  if (Number.isFinite(tool?.durationMs) && tool.durationMs >= 0) {
    return tool.durationMs;
  }
  const start = timestampToMs(
    tool?.timing?.started_at ?? tool?.startedEvent?.timestamp,
  );
  const end = timestampToMs(
    tool?.timing?.completed_at ?? tool?.resultEvent?.timestamp,
  );
  if (start === null || end === null || end < start) {
    return null;
  }
  return end - start;
}

function toolDisplay(tool) {
  const display =
    tool?.display ??
    tool?.toolCall?.display ??
    tool?.startedEvent?.payload?.display;
  return isPlainObject(display) ? display : null;
}

function toolDisplayFromEvent(event) {
  const display = event?.payload?.display;
  return isPlainObject(display) ? display : null;
}

function humanReadableToolLabel(toolName, argumentsValue) {
  let args = argumentsValue;
  if (typeof args === 'string') {
    try {
      args = JSON.parse(args);
    } catch {
      return args;
    }
  }

  if (!args || typeof args !== 'object' || Array.isArray(args)) {
    return typeof argumentsValue === 'string' ? argumentsValue.trim() : '';
  }

  if (TOOL_NO_SUMMARY_NAMES.has(toolName) || Object.keys(args).length === 0) {
    return '';
  }

  if (toolName === 'glob') {
    return searchToolLabel(args, false) ?? '';
  }

  if (toolName === 'grep') {
    return searchToolLabel(args, true) ?? '';
  }

  if (SUBAGENT_TOOL_NAMES.has(toolName)) {
    return subAgentToolLabel(toolName, args) ?? '';
  }

  const displayArgs = TOOL_DISPLAY_ARGS[toolName];
  if (displayArgs) {
    for (const key of displayArgs) {
      const value = args[key];
      if (typeof value === 'string' && value.trim() !== '') {
        return value;
      }
    }
  }

  if (toolNameHasHiddenArguments(toolName)) {
    return '';
  }

  const firstStringEntry = Object.values(args).find(
    (value) =>
      typeof value === 'string' &&
      value.length <= MAX_TOOL_LABEL_LENGTH &&
      value.trim() !== '',
  );
  return firstStringEntry ?? '';
}

function searchToolLabel(args, includePath) {
  const pattern = trimmedString(args.pattern);
  if (!pattern) {
    return null;
  }

  const path = includePath ? trimmedString(args.path) : '';
  return path ? `${pattern} · ${path}` : pattern;
}

function trimmedString(value) {
  if (typeof value !== 'string') {
    return '';
  }
  return value.trim();
}

function subAgentArguments(tool) {
  const parsedArguments = parseJsonValue(toolArguments(tool));
  return isPlainObject(parsedArguments) ? parsedArguments : {};
}

function subAgentResultEnvelope(tool) {
  const parsedResult = parseJsonValue(tool.result);
  return isPlainObject(parsedResult) ? parsedResult : {};
}

function subAgentResultData(tool) {
  const sessionData = isPlainObject(tool.subAgentSession)
    ? tool.subAgentSession
    : {};
  const resultEnvelope = subAgentResultEnvelope(tool);
  if (isPlainObject(resultEnvelope.data)) {
    return { ...sessionData, ...resultEnvelope.data };
  }
  if (isPlainObject(resultEnvelope)) {
    return { ...sessionData, ...resultEnvelope };
  }
  return sessionData;
}

function subAgentToolLabel(toolName, args) {
  const agentId = trimmedString(args.agent_id);
  const preview =
    toolName === 'subagent'
      ? truncateToolLabel(
          trimmedString(args.content),
          MAX_SUBAGENT_PREVIEW_LENGTH,
        )
      : truncateToolLabel(
          trimmedString(args.session_id),
          MAX_SUBAGENT_PREVIEW_LENGTH,
        );
  return [agentId, preview].filter(Boolean).join(' · ');
}

function matchingSubAgentResultStatus(tool, assistantRun) {
  if (toolNameForRunTool(tool) !== 'subagent') {
    return '';
  }

  const sessionId = subAgentSessionId(tool);
  if (!sessionId) {
    return '';
  }

  const matchingResultTool = (assistantRun?.tools ?? []).find(
    (candidate) =>
      toolNameForRunTool(candidate) === 'subagent_result' &&
      subAgentSessionId(candidate) === sessionId &&
      candidate.resultEvent,
  );
  if (!matchingResultTool) {
    return '';
  }

  return subAgentStatusToDotStatus(subAgentChildStatus(matchingResultTool));
}

function externalSubAgentStatus(tool, subAgentStatuses) {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  const runId = trimmedString(data.run_id) || trimmedString(args.run_id);
  if (runId) {
    const runStatus = subAgentStatusToDotStatus(
      trimmedString(subAgentStatuses[`run:${runId}`]).toLowerCase(),
    );
    if (runStatus) {
      return runStatus;
    }
  }

  const agentId = trimmedString(data.agent_id) || trimmedString(args.agent_id);
  const sessionId = subAgentSessionId(tool);
  if (!agentId || !sessionId) {
    return '';
  }
  return subAgentStatusToDotStatus(
    trimmedString(
      subAgentStatuses[`session:${agentId}::${sessionId}`],
    ).toLowerCase(),
  );
}

function subAgentChildStatus(tool) {
  const data = subAgentResultData(tool);
  const status = trimmedString(data.status).toLowerCase();
  if (status) {
    return status;
  }
  return trimmedString(tool.subAgentSession?.status).toLowerCase();
}

function subAgentStatusToDotStatus(status) {
  if (['running', 'queued'].includes(status)) {
    return 'running';
  }
  if (['failed', 'error'].includes(status)) {
    return 'failed';
  }
  if (status === 'cancelled') {
    return 'cancelled';
  }
  if (['completed', 'success'].includes(status)) {
    return 'success';
  }
  return '';
}

function truncateToolLabel(value, maxLength) {
  if (!value || value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, maxLength - 1)}…`;
}

function isPlainObject(value) {
  return Object.prototype.toString.call(value) === '[object Object]';
}

function parseJsonValue(value) {
  if (typeof value !== 'string') {
    return value;
  }

  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function dateKeyForDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function todayDateKey() {
  return dateKeyForDate(new Date());
}

function isTodayDateKey(dateKey) {
  return dateKey === todayDateKey();
}

function hasErrorResult(result) {
  if (!result || typeof result !== 'object') {
    return false;
  }

  return Boolean(
    result.error ||
    result.ok === false ||
    result.success === false ||
    ['error', 'failed'].includes(result.status),
  );
}

function hasToolResultError(event) {
  if (event.payload?.error) {
    return true;
  }

  const content = messageFromEvent(event)?.content;
  if (!content) {
    return false;
  }

  try {
    return hasErrorResult(JSON.parse(content));
  } catch {
    return false;
  }
}
