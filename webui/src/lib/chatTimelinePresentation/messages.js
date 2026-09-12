import { trimmedString, parseJsonValue } from './values.js';
import { t } from '$lib/i18n.js';
import { isPlainObject } from '$lib/values.js';
import { toolNameForRunTool } from './toolFacts.js';
import { timestampToMs, formatDurationMs } from './time.js';
import { getAttachmentUrl } from '$lib/api.js';

export const isUserItem = (item) =>
  item.type === 'assistant_run'
    ? false
    : item.type === 'message'
      ? item.message.role === 'user'
      : item.event.type === 'user_message_persisted';

export const isAssistantItem = (item) =>
  item.type === 'assistant_run'
    ? true
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

// Channel group messages carry a platform sender; the display name is data,
// not a translatable UI string.
const senderDisplayName = (message) =>
  trimmedString(message?.sender?.display_name);

export const labelForMessage = (message) => {
  if (message.role === 'user') {
    return (
      senderDisplayName(message) || t('chat.role.user', 'You')
    ).toUpperCase();
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
  if (event.type === 'run_interrupted') {
    return t('chat.event.interrupted', 'Run interrupted');
  }
  if (event.type === 'user_message_persisted') {
    const displayName = senderDisplayName(messageFromEvent(event));
    return (displayName || t('chat.role.user', 'You')).toUpperCase();
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

// Provider errors are persisted as "<prefix>: <status> <json-body>" with the
// human-readable message buried inside the JSON (usually `error.message`,
// sometimes top-level `message`). Splits such a text into a readable summary
// and the pretty-printed raw body for a collapsible details block. Texts
// without a parseable embedded JSON object stay summary-only.
export const errorMessagePresentation = (text) => {
  const fullText = typeof text === 'string' ? text.trim() : '';
  const jsonStart = fullText.indexOf('{');
  if (jsonStart === -1) {
    return { summary: fullText, details: '' };
  }

  let parsedBody;
  try {
    parsedBody = JSON.parse(fullText.slice(jsonStart));
  } catch {
    return { summary: fullText, details: '' };
  }
  if (!isPlainObject(parsedBody)) {
    return { summary: fullText, details: '' };
  }

  const prefix = fullText.slice(0, jsonStart).trim();
  const errorBody = isPlainObject(parsedBody.error)
    ? parsedBody.error
    : parsedBody;
  const metadata = isPlainObject(errorBody.metadata) ? errorBody.metadata : {};
  const summaryMessage = embeddedSummaryMessage(
    embeddedErrorMessage(parsedBody),
    metadata,
  );
  const summary = summaryMessage
    ? [prefix, summaryMessage].filter(Boolean).join(' ')
    : prefix || fullText;

  return { summary, details: JSON.stringify(parsedBody, null, 2) };
};

// The router's upstream detail (OpenRouter's `metadata.raw` names the real
// cause — which upstream model failed and why) replaces the often-generic
// router message ("Provider returned error") in the directly visible summary,
// so rate limits and upstream outages are readable without expanding details.
// Long remedy hints stay details-only; the upstream provider name is appended.
function embeddedSummaryMessage(providerMessage, metadata) {
  const upstreamDetail =
    typeof metadata.raw === 'string' && metadata.raw.trim()
      ? metadata.raw.trim()
      : '';
  const providerName =
    typeof metadata.provider_name === 'string' && metadata.provider_name.trim()
      ? metadata.provider_name.trim()
      : '';
  if (!upstreamDetail && !providerName) {
    return providerMessage;
  }
  const base = upstreamDetail || providerMessage;
  if (!providerName || base.includes(providerName)) {
    return base;
  }
  return `${base} ${t('chat.errorViaProvider', '(via {name})', { name: providerName })}`;
}

function embeddedErrorMessage(value) {
  if (!isPlainObject(value)) {
    return '';
  }
  const nested = embeddedErrorMessage(value.error);
  if (nested) {
    return nested;
  }
  if (typeof value.message === 'string' && value.message.trim()) {
    return value.message.trim();
  }
  return '';
}

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

export const isFileMentionContentBlock = (block) =>
  isPlainObject(block) &&
  block.type === 'file_mention' &&
  trimmedString(block.path) !== '';

// Status hint for a degraded mention snapshot; an inlined one needs none —
// the chip itself says the file rode along.
export const fileMentionStatusLabel = (block) => {
  if (block?.status === 'too_large') {
    return t(
      'chat.fileMention.tooLarge',
      'too large to attach — referenced by path',
    );
  }
  if (block?.status === 'not_text') {
    return t(
      'chat.fileMention.notText',
      'not a text file — referenced by path',
    );
  }
  if (block?.status === 'missing') {
    return t('chat.fileMention.missing', 'file was not found at send time');
  }
  return '';
};

export const attachmentUrlForBlock = (block) =>
  attachmentUrlForId(block?.attachment_id);

export const attachmentFilename = (block) =>
  trimmedString(block?.filename) ||
  t('chat.attachment.fileLabel', 'Attached file');

export const imageReferenceLabel = (block) => {
  const reference = block?.image_reference;
  if (Number.isInteger(reference) && reference > 0) {
    return t('chat.attachment.imageReference', 'Image {number}', {
      number: reference,
    });
  }
  return attachmentFilename(block);
};

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

export function timestampForItem(item) {
  if (item.type === 'message') {
    return item.message.timestamp;
  }
  if (item.type === 'assistant_run') {
    return item.timestamp ?? item.startTimestamp ?? item.endTimestamp;
  }
  if (
    item.type === 'compaction_separator' ||
    item.type === 'takeover_separator'
  ) {
    return item.timestamp;
  }
  return item.event?.timestamp;
}

// Transient command-output cards (/status, /help) interleaved with the
// timeline. A card renders after the timeline item it was anchored to at
// creation. When that anchor item is gone — a history reload replaces live
// Run ids with history ids — the card keeps its chronological position by
// creation time instead: after the last item whose timestamp predates the
// command. Cards created on an empty timeline stay at the top; cards with a
// lost anchor and no usable creation time fall back to the timeline end.
export function groupTransientCards(items, cards) {
  const itemIds = new Set((items ?? []).map((item) => item.id));
  const groups = {
    leading: [],
    byItemId: new Map(),
    byItemIndex: new Map(),
    trailing: [],
  };
  for (const card of cards ?? []) {
    if (card?.anchorId && itemIds.has(card.anchorId)) {
      const anchored = groups.byItemId.get(card.anchorId) ?? [];
      anchored.push(card);
      groups.byItemId.set(card.anchorId, anchored);
      continue;
    }
    if (card?.anchorId == null) {
      groups.leading.push(card);
      continue;
    }
    const anchorIndex = transientCardAnchorIndex(items, card.createdAt);
    if (anchorIndex < 0) {
      groups.trailing.push(card);
      continue;
    }
    const anchored = groups.byItemIndex.get(anchorIndex) ?? [];
    anchored.push(card);
    groups.byItemIndex.set(anchorIndex, anchored);
  }
  return groups;
}

function transientCardAnchorIndex(items, createdAt) {
  const createdAtMs = timestampToMs(createdAt);
  if (createdAtMs === null) {
    return -1;
  }
  let anchorIndex = -1;
  for (const [index, item] of (items ?? []).entries()) {
    const itemMs = timestampToMs(timestampForItem(item));
    if (itemMs !== null && itemMs <= createdAtMs) {
      anchorIndex = index;
    }
  }
  return anchorIndex;
}

// Compose the label for an `agent_takeover` divider. The persisted message's
// `content` is a JSON string `{"from":"<address>","to":"<address>"}` where each
// address is a bare id (identity agent) or `agent@projekt` (team agent). The
// addresses are raw data, not translatable strings — only the surrounding
// phrase is localized, with the two addresses woven in. Falls back to a plain
// label when either address is missing so a malformed entry still renders.
export const takeoverSeparatorLabel = (message) => {
  const { from, to } = parseTakeoverContent(message?.content);
  if (from && to) {
    return t('chat.takenOver', 'Taken over by {from} → {to}', { from, to });
  }
  return t('chat.takenOverGeneric', 'Session taken over');
};

// Compact Context token display: 254224 -> "254k", 1500000 -> "1.5m". The
// separator line is about the Context, so the unit word is omitted.
const formatCompactTokens = (value) => {
  if (!Number.isFinite(value) || value < 0) {
    return null;
  }
  if (value < 1000) {
    return String(value);
  }
  if (value < 1_000_000) {
    return `${Math.round(value / 1000)}k`;
  }
  return `${(value / 1_000_000).toFixed(1)}m`;
};

export const compactionSeparatorLabel = (item) => {
  if (item?.status === 'running') {
    return t(
      'chat.compactingCurrentConversation',
      'Compacting current conversation…',
    );
  }

  const usage = item?.message?.usage ?? {};
  const before =
    item?.contextTokensBefore ?? usage.context_tokens_before ?? null;
  const after = item?.contextTokensAfter ?? usage.context_tokens_after ?? null;
  const beforeLabel = formatCompactTokens(before);
  const afterLabel = formatCompactTokens(after);
  const durationLabel = formatDurationMs(item?.durationMs);
  if (durationLabel && beforeLabel && afterLabel) {
    return t(
      'chat.compactedWithTimingTokens',
      'Context compacted in {duration} · ~{before} → ~{after}',
      { duration: durationLabel, before: beforeLabel, after: afterLabel },
    );
  }
  if (durationLabel) {
    return t('chat.compactedWithTiming', 'Context compacted in {duration}', {
      duration: durationLabel,
    });
  }
  if (beforeLabel && afterLabel) {
    return t(
      'chat.compactedWithTokens',
      'Context compacted · ~{before} → ~{after}',
      {
        before: beforeLabel,
        after: afterLabel,
      },
    );
  }
  return t('chat.compacted', 'Context compacted');
};

export const compactionSummaryText = (item) =>
  typeof item?.message?.content === 'string' ? item.message.content : '';

function parseTakeoverContent(content) {
  const parsed = parseJsonValue(content);
  if (!isPlainObject(parsed)) {
    return { from: '', to: '' };
  }
  return {
    from: trimmedString(parsed.from),
    to: trimmedString(parsed.to),
  };
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
  if (event.type === 'run_interrupted') {
    return t('chat.runStatus.interrupted', 'Interrupted');
  }
  return '';
};

export const isToolEvent = (event) =>
  event.type === 'tool_call_started' || event.type === 'tool_call_result';

export const isRunningToolEvent = (event) => event.type === 'tool_call_started';

export const isFailedToolEvent = (event) =>
  event.type === 'tool_call_result' && hasToolResultError(event);

export const isTerminalEvent = (event) => event.type.startsWith('run_');

function isRenderableUserContentBlock(block) {
  return (
    isTextContentBlock(block) ||
    isMediaContentBlock(block) ||
    isFileContentBlock(block) ||
    isFileMentionContentBlock(block)
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

export function hasToolResultError(event) {
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

export function hasToolResultPartial(event) {
  const rawResult =
    event.payload?.result ?? messageFromEvent(event)?.content ?? null;
  const result = parseJsonValue(rawResult);
  return result?.ok === true && result?.data?.status === 'partial';
}
