import { getAttachmentUrl } from '$lib/api.js';
import { formatAgentAddress } from '$lib/agentAddress.js';
import {
  compactToolValue,
  toolDetailPresentation,
  toolNameHasHiddenArguments,
} from '$lib/chatToolDetails.js';
import { activeLocaleTag, t } from '$lib/i18n.js';

const TOOL_DISPLAY_ARGS = {
  read: ['path'],
  write: ['path'],
  edit: ['path'],
  bash: ['command'],
  glob: ['pattern'],
  grep: ['pattern', 'path'],
  subagent: ['action', 'id', 'agent_id', 'content'],
  web_fetch: ['url'],
  web_search: ['query'],
  process: ['action', 'session_id'],
  cron: ['action', 'id', 'agent_id', 'schedule_type'],
  channel_send: ['channel_id', 'message'],
  skill: ['name'],
};
const TOOL_NO_SUMMARY_NAMES = new Set(['status']);
export const DEFAULT_TOOL_PRIMARY_MAX_CHARACTERS = 64;
const TOOL_PATH_SEGMENT_LIMIT = 3;
const MAX_SUBAGENT_PREVIEW_LENGTH = 96;
const MAX_BACKGROUND_BASH_LABEL_LENGTH = 96;
const SUBAGENT_TOOL_NAMES = new Set(['subagent']);

export { compactToolValue, toolDetailPresentation };

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
  const providerMessage = embeddedErrorMessage(parsedBody);
  const summary = providerMessage
    ? [prefix, providerMessage].filter(Boolean).join(' ')
    : prefix || fullText;

  return { summary, details: JSON.stringify(parsedBody, null, 2) };
};

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
  return toolArgumentSummary(toolRowFromEvent(event));
};

export const toolRowFromEvent = (event) => {
  const resultEvent = event?.type === 'tool_call_result' ? event : null;
  return {
    name: toolNameForEvent(event),
    toolCall: toolCallFromEvent(event),
    display: toolDisplayFromEvent(event),
    startedEvent: resultEvent ? null : event,
    resultEvent,
    result: resultEvent?.payload?.result,
    timing: resultEvent?.payload?.timing ?? null,
    status: resultEvent
      ? hasToolResultError(event)
        ? 'failed'
        : 'success'
      : 'running',
  };
};

export const visibleRunChildren = (assistantRun) =>
  (assistantRun.items ?? []).filter((child) => {
    if (child.type === 'tool_call') {
      return shouldRenderToolCall(child);
    }
    if (child.type === 'compaction_separator') {
      return true;
    }
    return Boolean(child.content);
  });

// `streaming` means a text section still comes from transient deltas and may
// later be replaced by its stable message. It does not mean that section is
// still the Run's current activity: once a later visible child exists, the
// stream has advanced and only that later child may carry the working caret.
export function isRunChildWorking(assistantRun, child) {
  if (assistantRun?.status !== 'running' || !child?.streaming || !child?.id) {
    return false;
  }

  const children = visibleRunChildren(assistantRun);
  const childIndex = children.findIndex(
    (candidate) => candidate.id === child.id,
  );
  return childIndex >= 0 && childIndex === children.length - 1;
}

// Duration label for one reasoning block header: the persisted first-to-last
// reasoning delta span once the stable boundary arrived, otherwise the frozen
// estimate from when the deltas stopped growing, otherwise a live estimate
// ticking with the shared nowMs clock while the deltas still stream.
// Returns '' when nothing is measurable (no timing, no streamed start time).
export function reasoningDurationLabel(child, nowMs = Date.now()) {
  const durationMs = child?.durationMs;
  if (Number.isFinite(durationMs) && durationMs >= 0) {
    return formatDurationMs(durationMs);
  }
  const estimateMs = child?.durationEstimateMs;
  if (Number.isFinite(estimateMs) && estimateMs >= 0) {
    return formatDurationMs(estimateMs);
  }
  if (!child?.streaming) {
    return '';
  }
  const start = timestampToMs(child.timestamp);
  if (start === null) {
    return '';
  }
  return formatDurationMs(Math.max(0, nowMs - start));
}

// Footer line for an assistant run: status · duration · iterations · end
// time. The duration ticks live while the run is running (driven by the
// shared nowMs clock); the end time appears once the run reaches a terminal
// state (completion, cancellation, failure) and uses the same clock-time
// formatting as the message header timestamp. This is the stable first line
// of run-level meta information; the message header shows only the start
// timestamp. File-change statistics are appended to this same line via
// `changeStatsParts`, while transient problem notices (provider liveness)
// render on a separate line below via `runFooterNotice` so they can never
// push the stable parts onto a wrap line.
export const runFooterParts = (assistantRun, nowMs = Date.now()) => {
  const parts = [];
  parts.push(runStatusLabel(assistantRun.status));
  const duration = formatRunDuration(assistantRun, nowMs);
  if (duration) {
    parts.push(duration);
  }
  const iterationLabel = labelForRunIterations(assistantRun);
  if (iterationLabel) {
    parts.push(iterationLabel);
  }
  const endTimeLabel = runEndTimeLabel(assistantRun);
  if (endTimeLabel) {
    parts.push(endTimeLabel);
  }
  return parts;
};

// Transient problem/liveness notice for an assistant run, rendered on its own
// line below the footer. Returns '' when there is nothing to report.
export const runFooterNotice = (assistantRun) => {
  if (
    assistantRun.status === 'running' &&
    Number.isFinite(assistantRun.providerHeartbeat?.idleSeconds)
  ) {
    return t(
      'chat.providerWorking',
      'Provider connected · waiting {seconds}s for the next model chunk',
      { seconds: Math.round(assistantRun.providerHeartbeat.idleSeconds) },
    );
  }
  return '';
};

// Aggregated file-change statistics for one assistant run. Prefers the
// server-computed git-style values (real before/after line diffs — streamed
// live during the run and persisted on the run summary); a server-reported
// zero means genuinely no net changes and must NOT fall back to the per-call
// sum. The fallback covers runs from before the server tracker existed and
// sessions after a server restart. Returns null when the run contains no file
// changes.
export const runChangeStats = (assistantRun) => {
  const serverStats = serverChangeStats(assistantRun);
  if (serverStats) {
    return serverStats.files > 0 ? serverStats : null;
  }
  const { paths, added, removed } = collectRunChanges(assistantRun);
  if (paths.size === 0 && added === 0 && removed === 0) {
    return null;
  }
  return { files: paths.size, added, removed, paths: [...paths].sort() };
};

// Session-wide file-change statistics: the sum over every assistant run in the
// loaded timeline, with files deduplicated across runs. Returns null when the
// session contains no file changes.
export const sessionChangeStats = (timelineItems) => {
  const paths = new Set();
  let added = 0;
  let removed = 0;
  for (const item of timelineItems ?? []) {
    if (item?.type !== 'assistant_run') {
      continue;
    }
    const stats = runChangeStats(item);
    if (!stats) {
      continue;
    }
    for (const path of stats.paths ?? []) {
      paths.add(path);
    }
    added += stats.added;
    removed += stats.removed;
  }
  if (paths.size === 0 && added === 0 && removed === 0) {
    return null;
  }
  return { files: paths.size, added, removed, paths: [...paths].sort() };
};

// Validated server-computed change statistics carried by the run summary or
// the terminal run event. Returns null when absent or malformed.
function serverChangeStats(assistantRun) {
  const candidate = assistantRun?.changeStats;
  if (!isPlainObject(candidate)) {
    return null;
  }
  const files = candidate.files;
  const added = candidate.added;
  const removed = candidate.removed;
  if (
    !Number.isInteger(files) ||
    files < 0 ||
    !Number.isInteger(added) ||
    added < 0 ||
    !Number.isInteger(removed) ||
    removed < 0
  ) {
    return null;
  }
  const paths = Array.isArray(candidate.paths)
    ? candidate.paths.filter((path) => typeof path === 'string' && path)
    : [];
  return { files, added, removed, paths };
}

// Compact one-line label for change statistics, e.g. "3 files changed, +151 -15".
export const changeStatsLabel = (stats) => {
  if (!stats) {
    return '';
  }
  const fileLabel =
    stats.files === 1
      ? t('chat.changeStats.filesOne', '1 file changed')
      : t('chat.changeStats.filesMany', '{count} files changed', {
          count: stats.files,
        });
  return `${fileLabel}, +${stats.added} -${stats.removed}`;
};

// Structured change-stat parts for colored rendering: the file-count label
// (carrying the trailing comma) plus separate added/removed line counts.
// Rendered as one contiguous block, e.g. "6 files changed, +497 -387".
// Returns an empty array when the run changed no files.
export const changeStatsParts = (stats) => {
  if (!stats) {
    return [];
  }
  const fileLabel =
    stats.files === 1
      ? t('chat.changeStats.filesOne', '1 file changed')
      : t('chat.changeStats.filesMany', '{count} files changed', {
          count: stats.files,
        });
  return [
    { kind: 'files', text: `${fileLabel},` },
    { kind: 'added', text: `+${stats.added}` },
    { kind: 'removed', text: `-${stats.removed}` },
  ];
};

// Multi-line tooltip text listing every changed file of the run, one per line.
// Empty when the stats carry no paths (or are null), which disables the hint.
export const changeStatsTooltip = (stats) => {
  if (!stats || !Array.isArray(stats.paths) || stats.paths.length === 0) {
    return '';
  }
  return stats.paths.join('\n');
};

function collectRunChanges(assistantRun) {
  const paths = new Set();
  let added = 0;
  let removed = 0;
  for (const child of visibleRunChildren(assistantRun)) {
    if (child?.type !== 'tool_call') {
      continue;
    }
    const name = toolNameForRunTool(child);
    if (name !== 'edit' && name !== 'write') {
      continue;
    }
    let childAdded = 0;
    let childRemoved = 0;
    for (const fact of toolDisplayFacts(child)) {
      if (fact.kind === 'line_change' && fact.change === 'added') {
        childAdded += fact.value;
      } else if (fact.kind === 'line_change' && fact.change === 'removed') {
        childRemoved += fact.value;
      }
    }
    if (childAdded === 0 && childRemoved === 0) {
      continue;
    }
    const path = toolChangePath(child);
    if (path) {
      paths.add(path);
    }
    added += childAdded;
    removed += childRemoved;
  }
  return { paths, added, removed };
}

function toolDisplayFacts(tool) {
  const display = toolDisplay(tool);
  return Array.isArray(display?.facts) ? display.facts : [];
}

function toolChangePath(tool) {
  const args = toolArguments(tool);
  if (isPlainObject(args) && typeof args.path === 'string' && args.path) {
    return args.path;
  }
  const display = toolDisplay(tool);
  const primary = Array.isArray(display?.primary) ? display.primary : [];
  for (const part of primary) {
    if (isPlainObject(part) && part.kind === 'path') {
      const value = trimmedString(part.full_value) || trimmedString(part.value);
      if (value) {
        return value;
      }
    }
  }
  return '';
}

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

// A tool row the model is still *streaming* — the call has been previewed from
// its argument deltas but not dispatched yet (no `tool_call_started`). It shares
// the `running` bucket in `toolStatus` (both are "not settled"), but the dot must
// read differently: a preparing call has not begun executing, so an early
// streamed sibling in a parallel batch should not look like it has been running
// for ages. `mergeToolStarted` clears this to `running` the moment it dispatches.
export const isToolPreparing = (tool) => tool?.status === 'preparing';

export const toolStatusLabel = (tool, nowMs = Date.now()) => {
  if (toolStatus(tool) === 'cancelled') {
    const duration = formatDurationMs(
      toolDurationMs(tool),
      'chat.toolDurationSeconds',
    );
    return [t('chat.toolCancelled', 'cancelled'), duration]
      .filter(Boolean)
      .join(' · ');
  }
  if (toolStatus(tool) === 'running') {
    if (isToolPreparing(tool)) {
      return '';
    }
    return formatDurationMs(
      elapsedSinceTimestamp(toolStartedTimestamp(tool), nowMs),
      'chat.toolDurationSeconds',
    );
  }
  return formatDurationMs(toolDurationMs(tool), 'chat.toolDurationSeconds');
};

// Real wall-clock runtime of the child run a sub-agent tool refers to, captured
// from the child run's terminal lifecycle event. Session-keyed fallback applies
// only when no run id is known: a child session can be reused by later spawns,
// so a session-scoped duration may describe a different run than this row's.
// Returns null when no child duration was tracked yet.
export const subAgentRunDurationMs = (tool, subAgentStatuses = {}) => {
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    const durationMs = statuses[`runDuration:${runId}`];
    return Number.isFinite(durationMs) && durationMs >= 0 ? durationMs : null;
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (agentId && sessionId) {
    const durationMs = statuses[`sessionDuration:${agentId}::${sessionId}`];
    if (Number.isFinite(durationMs) && durationMs >= 0) {
      return durationMs;
    }
  }

  return null;
};

export const subAgentRunStartedAt = (tool, subAgentStatuses = {}) => {
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    return trimmedString(statuses[`runStarted:${runId}`]);
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (agentId && sessionId) {
    return trimmedString(statuses[`sessionStarted:${agentId}::${sessionId}`]);
  }
  return '';
};

export const assistantRunNeedsLiveClock = (
  assistantRun,
  subAgentStatuses = {},
) => {
  if (
    assistantRun?.status === 'running' &&
    timestampToMs(assistantRun.startTimestamp ?? assistantRun.timestamp) !==
      null
  ) {
    return true;
  }
  return (assistantRun?.items ?? []).some((tool) => {
    if (tool?.type !== 'tool_call') {
      return false;
    }
    if (isSubAgentSpawnTool(tool)) {
      return (
        subAgentDotStatus(tool, subAgentStatuses) === 'running' &&
        timestampToMs(subAgentRunStartedAt(tool, subAgentStatuses)) !== null
      );
    }
    return (
      toolStatus(tool) === 'running' &&
      !isToolPreparing(tool) &&
      timestampToMs(toolStartedTimestamp(tool)) !== null
    );
  });
};

export const liveClockCadenceMs = (
  timelineItems,
  subAgentStatuses = {},
  nowMs = Date.now(),
) => {
  const starts = [];
  for (const assistantRun of timelineItems ?? []) {
    if (assistantRun?.type !== 'assistant_run') {
      continue;
    }
    if (assistantRun.status === 'running') {
      starts.push(assistantRun.startTimestamp ?? assistantRun.timestamp);
    }
    for (const tool of assistantRun.items ?? []) {
      if (tool?.type !== 'tool_call') {
        continue;
      }
      if (isSubAgentSpawnTool(tool)) {
        if (subAgentDotStatus(tool, subAgentStatuses) === 'running') {
          starts.push(subAgentRunStartedAt(tool, subAgentStatuses));
        }
      } else if (toolStatus(tool) === 'running' && !isToolPreparing(tool)) {
        starts.push(toolStartedTimestamp(tool));
      }
    }
  }
  const validStarts = starts
    .map(timestampToMs)
    .filter((value) => value !== null);
  if (validStarts.length === 0) {
    return 0;
  }
  return validStarts.some(
    (startedAt) => Math.max(0, nowMs - startedAt) < 10_000,
  )
    ? 100
    : 1000;
};

// Name of the most recent tool call the child run made, recorded from bridged
// child `tool_call_started` events. Resolved strictly by run id when one is
// known — a session-keyed entry may describe a different run of a reused child
// session (B6) — with the session key only as the run-id-less fallback.
// Returns '' when the child has made no tool call yet (or the entry was
// reset on run start / evicted from the capped projection).
export const subAgentLastToolName = (tool, subAgentStatuses = {}) => {
  if (!isSubAgentSpawnTool(tool)) {
    return '';
  }
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    return trimmedString(statuses[`runTool:${runId}`]);
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (agentId && sessionId) {
    return trimmedString(statuses[`sessionTool:${agentId}::${sessionId}`]);
  }
  return '';
};

// Status label for a sub-agent tool row. Prefers the child run's real runtime
// over the spawn tool's own call duration, which is ~0s for a background spawn
// that returns the moment the child run starts.
export const subAgentToolStatusLabel = (
  tool,
  dotStatus,
  subAgentStatuses = {},
  nowMs = Date.now(),
) => {
  if (dotStatus === 'cancelled') {
    const duration = formatDurationMs(
      subAgentRunDurationMs(tool, subAgentStatuses),
      'chat.toolDurationSeconds',
    );
    return [t('chat.toolCancelled', 'cancelled'), duration]
      .filter(Boolean)
      .join(' · ');
  }
  if (dotStatus === 'running') {
    return formatDurationMs(
      elapsedSinceTimestamp(
        subAgentRunStartedAt(tool, subAgentStatuses),
        nowMs,
      ),
      'chat.toolDurationSeconds',
    );
  }

  const childDurationMs = subAgentRunDurationMs(tool, subAgentStatuses);
  if (childDurationMs !== null) {
    return formatDurationMs(childDurationMs, 'chat.toolDurationSeconds');
  }

  // A background spawn carries no inline result, so its own duration is just
  // the spawn-call time; show nothing rather than a misleading near-zero.
  if (
    toolNameForRunTool(tool) === 'subagent' &&
    !trimmedString(subAgentResultData(tool).result)
  ) {
    return '';
  }
  return formatDurationMs(toolDurationMs(tool), 'chat.toolDurationSeconds');
};

// Single source of truth for whether a tool/sub-agent row should render a
// per-row cancel control. The row shape is intentionally narrow so the rule
// stays testable and easy to extend (e.g. when other tools gain a cancel).
export const isRowCancellable = (row) => {
  if (!isPlainObject(row)) {
    return false;
  }
  if (row.kind === 'tool_call') {
    // A streaming row only previews a tool call the model is still writing;
    // there is no dispatched call to cancel yet.
    return (
      row.toolName === 'bash' &&
      row.toolStatus === 'running' &&
      row.streaming !== true
    );
  }
  if (row.kind === 'sub_agent') {
    return row.dotStatus === 'running';
  }
  return false;
};

export const toolArguments = (tool) =>
  tool.arguments ?? tool.toolCall?.arguments ?? streamingPreviewArguments(tool);

export const toolArgumentSummary = (tool) => {
  return toolRowPresentation(tool)
    .primary.map((part) => part.text)
    .filter(Boolean)
    .join(' · ');
};

export const toolRowPresentation = (tool) => {
  const display = toolDisplay(tool);
  const structuredPrimary = Array.isArray(display?.primary)
    ? display.primary.map(toolPrimaryPart).filter(Boolean).slice(0, 2)
    : [];
  const displaySummary = trimmedString(display?.summary);
  const legacySummary =
    displaySummary ||
    humanReadableToolLabel(
      toolNameForRunTool(tool),
      toolArguments(tool) ?? streamingPreviewArguments(tool),
    );
  const primary =
    structuredPrimary.length > 0
      ? structuredPrimary
      : legacySummary
        ? [
            toolPrimaryPart({
              kind: 'text',
              value: legacySummary,
              full_value: legacySummary,
              truncate: 'end',
              tooltip: 'truncated',
              max_characters: DEFAULT_TOOL_PRIMARY_MAX_CHARACTERS,
            }),
          ].filter(Boolean)
        : [];
  const facts = Array.isArray(display?.facts)
    ? display.facts.map(toolFactPresentation).filter(Boolean)
    : [];
  return { primary, facts };
};

function toolPrimaryPart(part) {
  if (!isPlainObject(part)) {
    return null;
  }
  const fullText = trimmedString(part.full_value) || trimmedString(part.value);
  if (!fullText) {
    return null;
  }
  const kind = trimmedString(part.kind) || 'text';
  const truncate = ['start', 'end', 'middle', 'never'].includes(part.truncate)
    ? part.truncate
    : 'end';
  const maxCharacters =
    Number.isInteger(part.max_characters) && part.max_characters > 0
      ? part.max_characters
      : DEFAULT_TOOL_PRIMARY_MAX_CHARACTERS;
  const sourceValue =
    kind === 'path' ? compactToolPath(trimmedString(part.value)) : fullText;
  const text = truncateSemanticValue(sourceValue, truncate, maxCharacters);
  const tooltipMode = ['always', 'none', 'truncated'].includes(part.tooltip)
    ? part.tooltip
    : 'truncated';
  const showTooltip =
    tooltipMode === 'always' ||
    (tooltipMode === 'truncated' && text !== fullText);
  return {
    kind,
    text,
    fullText,
    truncate,
    copyable: part.copyable === true,
    tooltipText: showTooltip ? fullText : '',
  };
}

function toolFactPresentation(fact) {
  if (
    isPlainObject(fact) &&
    fact.kind === 'line_range' &&
    Number.isInteger(fact.start) &&
    fact.start >= 1 &&
    Number.isInteger(fact.end) &&
    fact.end >= fact.start
  ) {
    return {
      kind: 'line_range',
      text: t('chat.toolFact.lines', 'lines {start}-{end}', {
        start: fact.start,
        end: fact.end,
      }),
      variant: 'neutral',
    };
  }
  if (
    isPlainObject(fact) &&
    fact.kind === 'line_change' &&
    Number.isInteger(fact.value) &&
    fact.value >= 0 &&
    ['added', 'removed'].includes(fact.change)
  ) {
    return {
      kind: 'line_change',
      text: `${fact.change === 'added' ? '+' : '-'}${fact.value}`,
      variant: fact.change,
    };
  }
  if (
    !isPlainObject(fact) ||
    fact.kind !== 'count' ||
    !Number.isInteger(fact.value) ||
    fact.value < 0 ||
    !['matches', 'results'].includes(fact.unit)
  ) {
    return null;
  }
  const renderedCount = `${fact.value}${fact.at_least === true ? '+' : ''}`;
  const singular = fact.value === 1 && fact.at_least !== true;
  const label =
    fact.unit === 'matches'
      ? singular
        ? t('chat.toolFact.match', '{count} match', { count: renderedCount })
        : t('chat.toolFact.matches', '{count} matches', {
            count: renderedCount,
          })
      : singular
        ? t('chat.toolFact.result', '{count} result', {
            count: renderedCount,
          })
        : t('chat.toolFact.results', '{count} results', {
            count: renderedCount,
          });
  return { kind: 'count', text: label, variant: 'neutral' };
}

export function compactToolPath(value) {
  const normalized = trimmedString(value).replaceAll('\\', '/');
  if (!normalized) {
    return '';
  }
  const absolute =
    normalized.startsWith('/') || /^[A-Za-z]:\//.test(normalized);
  const segments = normalized.split('/').filter(Boolean);
  if (!absolute && segments.length <= TOOL_PATH_SEGMENT_LIMIT) {
    return normalized;
  }
  if (segments.length <= TOOL_PATH_SEGMENT_LIMIT) {
    return normalized;
  }
  return `…/${segments.slice(-TOOL_PATH_SEGMENT_LIMIT).join('/')}`;
}

export function truncateSemanticValue(
  value,
  mode,
  maxCharacters = DEFAULT_TOOL_PRIMARY_MAX_CHARACTERS,
) {
  const text = typeof value === 'string' ? value : '';
  if (
    mode === 'never' ||
    !Number.isInteger(maxCharacters) ||
    maxCharacters < 1 ||
    text.length <= maxCharacters
  ) {
    return text;
  }
  if (maxCharacters === 1) {
    return '…';
  }
  if (mode === 'start') {
    return `…${text.slice(-(maxCharacters - 1))}`;
  }
  if (mode === 'middle') {
    const available = maxCharacters - 1;
    const prefixLength = Math.ceil(available / 2);
    const suffixLength = Math.floor(available / 2);
    return `${text.slice(0, prefixLength)}…${text.slice(-suffixLength)}`;
  }
  return `${text.slice(0, maxCharacters - 1)}…`;
}

// While a tool call is still streaming, the completed top-level string fields
// extracted from the partial arguments JSON stand in for the parsed arguments
// so the preparing row can already show e.g. a write's file path.
const streamingPreviewArguments = (tool) =>
  isPlainObject(tool?.previewArguments) &&
  Object.keys(tool.previewArguments).length > 0
    ? tool.previewArguments
    : undefined;

export const isSubAgentSpawnTool = (tool) => {
  if (toolNameForRunTool(tool) !== 'subagent') {
    return false;
  }
  const args = subAgentArguments(tool);
  if (args.action) {
    return args.action === 'run';
  }
  // Persisted history from before the action contract had no top-level action.
  return args.operation !== 'cancel';
};

export const isBackgroundSubAgentSpawn = (tool) => {
  if (!isSubAgentSpawnTool(tool)) {
    return false;
  }
  const delivery = trimmedString(subAgentResultData(tool).delivery);
  if (delivery) {
    return delivery === 'automatic';
  }
  return subAgentArguments(tool).background === true;
};

export const isStartingForegroundSubAgent = (tool) => {
  if (!isSubAgentSpawnTool(tool) || !tool.startedEvent) {
    return false;
  }
  const delivery = trimmedString(subAgentResultData(tool).delivery);
  if (delivery) {
    return delivery === 'inline';
  }
  return subAgentArguments(tool).background !== true;
};

const subAgentSessionId = (tool) => {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  return trimmedString(data.session_id) || trimmedString(args.session_id);
};

export const subAgentAgentId = (tool) => {
  return subAgentTargetAddress(tool) || t('common.unknown', 'Unknown');
};

const subAgentTargetAddress = (tool) => {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  const dataAgentId = trimmedString(data.agent_id);
  if (dataAgentId) {
    const projectId = trimmedString(data.project_id);
    return projectId ? formatAgentAddress(dataAgentId, projectId) : dataAgentId;
  }
  return trimmedString(args.agent_id);
};

const subAgentRunId = (tool) => {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  return trimmedString(data.run_id) || trimmedString(args.run_id);
};

// The child run id this spawn row refers to. A queued spawn's descriptor only
// carries a queue_item_id; once the queued run starts, the run stream records a
// `queueRun:<queue_item_id>` → run_id mapping (from the run_started payload),
// which resolves the row to its own run even though the persisted descriptor
// never learns the run id.
export const subAgentEffectiveRunId = (tool, subAgentStatuses = {}) => {
  const runId = subAgentRunId(tool);
  if (runId) {
    return runId;
  }
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const workId = trimmedString(subAgentResultData(tool).id);
  if (workId) {
    const inspectedRunId = trimmedString(statuses[`workRun:${workId}`]);
    if (inspectedRunId) {
      return inspectedRunId;
    }
  }
  const queueItemId = subAgentQueueItemId(tool);
  if (!queueItemId) {
    return '';
  }
  return trimmedString(statuses[`queueRun:${queueItemId}`]);
};

export const subAgentQueueItemId = (tool) =>
  trimmedString(subAgentResultData(tool).queue_item_id);

// Decides how a sub-agent spawn row's cancel button acts. A resolvable child
// run id — from the frozen descriptor or, for a spawn that left the queue,
// the `queueRun:<item>` mapping (B6) — is cancelled as a run; a queued spawn
// without a resolvable run id is removed from the child session's queue.
// `null` means the row addresses nothing cancellable (the caller may still
// verify server-side).
export const resolveSubAgentCancelPlan = (tool, subAgentStatuses = {}) => {
  if (!isPlainObject(tool)) {
    return null;
  }
  const runId = subAgentEffectiveRunId(tool, subAgentStatuses);
  if (runId) {
    return { kind: 'run', runId };
  }
  const target = subAgentNavigationTarget(tool);
  const queueItemId = subAgentQueueItemId(tool);
  if (target && queueItemId) {
    return {
      kind: 'queue',
      queueItemId,
      agentId: target.agentId,
      sessionId: target.sessionId,
    };
  }
  return null;
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

export const subAgentDotStatus = (tool, subAgentStatuses = {}) => {
  const parentStatus = toolStatus(tool);
  if (['failed', 'cancelled'].includes(parentStatus)) {
    return parentStatus;
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
  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (!agentId || !sessionId) {
    return null;
  }
  return { agentId, sessionId };
};

// Cache key for a spawn row's fetched result. Keyed by the child run when it is
// known so repeated spawns into the same child session each fetch their own
// final output; the session-level key is only the fallback for rows without a
// resolvable run id.
export const subAgentResultKey = (tool, subAgentStatuses = {}) => {
  const workId = trimmedString(subAgentResultData(tool).id);
  if (workId) {
    return `work:${workId}`;
  }
  const target = subAgentNavigationTarget(tool);
  if (!target) {
    return '';
  }
  const runId = subAgentEffectiveRunId(tool, subAgentStatuses);
  return runId
    ? `${target.agentId}::${target.sessionId}::${runId}`
    : `${target.agentId}::${target.sessionId}`;
};

// A failed result fetch must not be cached forever (a transient inspection
// error would otherwise permanently blank the Result row), but deleting the
// entry would retrigger the fetch effect in a tight loop. Instead failed
// entries carry `error`/`failedAt` and become fetchable again after a cooldown,
// so the next natural re-render retries.
const SUBAGENT_RESULT_RETRY_DELAY_MS = 15000;

export const subAgentResultEntryAllowsFetch = (entry, now = Date.now()) => {
  if (!entry) {
    return true;
  }
  if (entry.error && !entry.loading) {
    const failedAt = Number.isFinite(entry.failedAt) ? entry.failedAt : 0;
    return now - failedAt >= SUBAGENT_RESULT_RETRY_DELAY_MS;
  }
  return false;
};

// A background spawn returns a "running" descriptor as its tool result, so the
// final output never lands in tool.result. Foreground spawns already carry it as
// data.result. When the child run has finished and no inline result exists, the
// durable work result must be inspected to show the response.
export const subAgentShouldFetchResult = (tool, dotStatus) => {
  if (!isSubAgentSpawnTool(tool)) {
    return false;
  }
  if (dotStatus !== 'success') {
    return false;
  }
  if (trimmedString(subAgentResultData(tool).result)) {
    return false;
  }
  const delivery = trimmedString(subAgentResultData(tool).delivery);
  if (
    delivery !== 'automatic' &&
    !(delivery === '' && subAgentArguments(tool).background === true)
  ) {
    return false;
  }
  return Boolean(subAgentNavigationTarget(tool));
};

// The sub-agent dot falls back to the frozen persisted descriptor's `status`
// (subAgentResultData.status) when no external `run:`/`session:` status has
// arrived. That fallback is what produces a "running" dot forever after a missed
// terminal event, a rolled replay buffer, or a server restart. When neither the
// `run:<run_id>` nor the `session:<agent_id>::<session_id>` key has been seen
// at all, the only thing telling us the child is still running is that frozen
// descriptor, and the Chat controller should verify the durable work record.
export const subAgentNeedsStatusVerification = (
  tool,
  dotStatus,
  subAgentStatuses = {},
) => {
  if (dotStatus !== 'running') {
    return false;
  }
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};

  // With a known run id only the run-scoped key counts: a session-scoped
  // status may belong to a different run of the same (reused) child session,
  // so it must neither settle this row nor suppress its verification.
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    return !Object.prototype.hasOwnProperty.call(statuses, `run:${runId}`);
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (
    agentId &&
    sessionId &&
    Object.prototype.hasOwnProperty.call(
      statuses,
      `session:${agentId}::${sessionId}`,
    )
  ) {
    return false;
  }

  return true;
};

// Returns the value to render in the tool's Result row. With a fetched result it
// rebuilds the same tool_success envelope a foreground spawn produces, so the
// fetched output renders identically; otherwise the original tool.result stands.
export const subAgentDisplayResult = (tool, fetchedResult = null) => {
  const resultText = trimmedString(fetchedResult?.result);
  if (!resultText) {
    return tool.result;
  }
  const target = subAgentNavigationTarget(tool) ?? {};
  const data = subAgentResultData(tool);
  const payload = {
    id: trimmedString(data.id),
    agent_id: target.agentId ?? trimmedString(data.agent_id),
    session_id: target.sessionId ?? subAgentSessionId(tool),
    status: 'completed',
    result: resultText,
  };
  const projectId = trimmedString(data.project_id);
  if (projectId) {
    payload.project_id = projectId;
  }
  if (fetchedResult?.usage) {
    payload.usage = fetchedResult.usage;
  }
  return { ok: true, error: null, data: payload, artifacts: [] };
};

export const backgroundTasks = (
  timelineItems,
  subAgentStatuses = {},
  backgroundBashStatuses = {},
) => {
  const tasks = [];
  let order = 0;
  for (const [itemIndex, item] of (timelineItems ?? []).entries()) {
    if (item?.type !== 'assistant_run') {
      continue;
    }
    for (const [childIndex, child] of visibleRunChildren(item).entries()) {
      if (child?.type !== 'tool_call') {
        continue;
      }
      if (isBackgroundSubAgentSpawn(child)) {
        const dotStatus = subAgentDotStatus(child, subAgentStatuses);
        tasks.push({
          id: `${item.id ?? itemIndex}:${child.id ?? child.toolCallId ?? childIndex}`,
          kind: 'subagent',
          tool: child,
          dotStatus,
          label: subAgentAgentId(child),
          agentId: subAgentAgentId(child),
          preview: subAgentPreview(child),
          target: subAgentNavigationTarget(child),
          lastToolName:
            dotStatus === 'running'
              ? subAgentLastToolName(child, subAgentStatuses)
              : '',
          timeLabel: subAgentToolStatusLabel(
            child,
            dotStatus,
            subAgentStatuses,
          ),
          order,
        });
        order += 1;
        continue;
      }
      const bashTask = backgroundBashTask(child, backgroundBashStatuses);
      if (!bashTask) {
        continue;
      }
      tasks.push({
        id: `bash:${bashTask.processSessionId}`,
        kind: 'bash',
        tool: child,
        dotStatus: bashTask.dotStatus,
        label: bashTask.command,
        command: bashTask.command,
        processSessionId: bashTask.processSessionId,
        target: null,
        order,
      });
      order += 1;
    }
  }

  return tasks
    .sort((left, right) => {
      const activeDifference =
        Number(right.dotStatus === 'running') -
        Number(left.dotStatus === 'running');
      return activeDifference || right.order - left.order;
    })
    .map(({ order: _order, ...task }) => task);
};

function backgroundBashTask(tool, backgroundBashStatuses) {
  if (toolNameForRunTool(tool) !== 'bash') {
    return null;
  }
  const envelope = parseJsonValue(tool.result);
  const data = isPlainObject(envelope?.data) ? envelope.data : {};
  const processSessionId = trimmedString(data.session_id);
  if (data.delivery !== 'automatic' || !processSessionId) {
    return null;
  }
  const args = parseJsonValue(toolArguments(tool));
  const command = truncateToolLabel(
    trimmedString(isPlainObject(args) ? args.command : '').replace(/\s+/g, ' '),
    MAX_BACKGROUND_BASH_LABEL_LENGTH,
  );
  const durableStatus = isPlainObject(backgroundBashStatuses)
    ? backgroundBashStatuses[processSessionId]
    : '';
  return {
    command: command || t('chat.activity.bashFallback', 'Bash process'),
    processSessionId,
    dotStatus: backgroundBashDotStatus(durableStatus || data.status),
  };
}

function backgroundBashDotStatus(status) {
  if (status === 'completed' || status === 'success') {
    return 'success';
  }
  if (status === 'failed') {
    return 'failed';
  }
  if (status === 'killed' || status === 'cancelled') {
    return 'cancelled';
  }
  return 'running';
}

// Reflection reviews execute in a same-Agent fork whose lifecycle events carry
// the reviewed source session. These helpers map the run-kind vocabulary onto
// Activity-panel presentation and project the per-source tracking entries
// written by chatRunStream into sortable panel rows.
export const REFLECTION_RUN_KIND_SCOPES = {
  memory_reflection: 'memory',
  skill_reflection: 'skill',
  reflection: 'combined',
};

export const isReflectionRunKind = (runKind) =>
  typeof runKind === 'string' && runKind in REFLECTION_RUN_KIND_SCOPES;

export const reflectionScopeForRunKind = (runKind) =>
  isReflectionRunKind(runKind) ? REFLECTION_RUN_KIND_SCOPES[runKind] : '';

export const reflectionTaskRows = (sessionState) => {
  const entries = isPlainObject(sessionState?.reflectionTasks)
    ? sessionState.reflectionTasks
    : {};
  return Object.entries(entries)
    .filter(
      ([, entry]) =>
        isPlainObject(entry) &&
        typeof entry.sessionId === 'string' &&
        entry.sessionId.length > 0,
    )
    .map(([runId, entry]) => ({
      runId,
      sessionId: entry.sessionId,
      runKind: entry.runKind,
      scope: reflectionScopeForRunKind(entry.runKind),
      status:
        typeof entry.status === 'string' && entry.status
          ? entry.status
          : 'running',
      startedAt: typeof entry.startedAt === 'string' ? entry.startedAt : '',
    }))
    .sort((left, right) => {
      const activeDifference =
        Number(right.status === 'running') - Number(left.status === 'running');
      return (
        activeDifference ||
        (Date.parse(right.startedAt) || 0) - (Date.parse(left.startedAt) || 0)
      );
    });
};

// Coarse elapsed time for a running review; the panel re-renders it from a
// ticking clock only while the panel is open with running reflections.
export const reflectionElapsedLabel = (startedAt, nowMs) => {
  const startedMs = Date.parse(startedAt);
  if (Number.isNaN(startedMs) || !Number.isFinite(nowMs)) {
    return '';
  }
  const elapsedMs = Math.max(0, nowMs - startedMs);
  if (elapsedMs < 60_000) {
    return t('chat.activity.reflectionElapsedSeconds', '{count}s', {
      count: Math.floor(elapsedMs / 1000),
    });
  }
  return t('chat.activity.reflectionElapsedMinutes', '{count}m', {
    count: Math.floor(elapsedMs / 60_000),
  });
};

// Extracts one terminal sub-agent Run's final response. A visible Assistant turn
// is not final until its exact Run Summary follows it.
export const subAgentResultTextFromMessages = (messages, runId = '') => {
  if (!Array.isArray(messages)) {
    return '';
  }

  let summaryIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (
      !isPlainObject(message) ||
      message.role !== 'run_summary' ||
      (runId && message.run_id !== runId)
    ) {
      continue;
    }
    summaryIndex = index;
    break;
  }
  if (summaryIndex < 0) {
    return '';
  }

  if (
    !runId &&
    messages
      .slice(summaryIndex + 1)
      .some((message) =>
        ['user', 'assistant', 'tool', 'error'].includes(message?.role),
      )
  ) {
    return '';
  }

  let segmentStart = 0;
  for (let index = summaryIndex - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === 'run_summary') {
      segmentStart = index + 1;
      break;
    }
  }

  for (let index = summaryIndex - 1; index >= segmentStart; index -= 1) {
    const message = messages[index];
    if (!isPlainObject(message) || message.role !== 'assistant') {
      continue;
    }
    const text = assistantMessageText(message.content);
    if (text) {
      return text;
    }
  }
  return '';
};

function assistantMessageText(content) {
  if (typeof content === 'string') {
    return content.trim();
  }
  if (Array.isArray(content)) {
    return content
      .filter((block) => isTextContentBlock(block))
      .map((block) => block.text.trim())
      .join('\n\n')
      .trim();
  }
  return '';
}

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
  return new Intl.DateTimeFormat(activeLocaleTag(), {
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
  return new Intl.DateTimeFormat(activeLocaleTag(), {
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
  if (
    item.type === 'compaction_separator' ||
    item.type === 'takeover_separator'
  ) {
    return item.timestamp;
  }
  return item.event?.timestamp;
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
  if (Number.isFinite(before) && Number.isFinite(after)) {
    const numberFormat = new Intl.NumberFormat(activeLocaleTag());
    return t(
      'chat.compactedWithTokens',
      'Context compacted · ~{before} → ~{after} tokens',
      {
        before: numberFormat.format(before),
        after: numberFormat.format(after),
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

export const toolNameForRunTool = (tool) =>
  tool.name || tool.toolCall?.name || t('chat.toolPendingName', 'tool');

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

function shouldRenderToolCall(tool) {
  if (isSubAgentSpawnTool(tool)) {
    return Boolean(
      subAgentNavigationTarget(tool) ||
      tool.resultEvent ||
      isStartingForegroundSubAgent(tool),
    );
  }
  return Boolean(
    tool.startedEvent ||
    tool.resultEvent ||
    tool.stdout ||
    tool.stderr ||
    tool.streaming,
  );
}

function labelForRunIterations(assistantRun) {
  const iterationCount = assistantRun?.iterationCount;
  if (!Number.isInteger(iterationCount) || iterationCount < 0) {
    return '';
  }
  return t('chat.runIterations', '{count} iter', {
    count: iterationCount,
  });
}

function runStatusLabel(status) {
  if (status === 'failed') {
    return t('chat.runStatus.failed', 'Failed');
  }
  if (status === 'cancelled') {
    return t('chat.runStatus.cancelled', 'Cancelled');
  }
  if (status === 'interrupted') {
    return t('chat.runStatus.interrupted', 'Interrupted');
  }
  if (status === 'completed' || status === 'success') {
    return t('chat.runStatus.completed', 'Completed');
  }
  return t('chat.runStatus.running', 'Running');
}

// Clock time when the run ended (e.g. "7:20 PM"), shown once the run reached
// a terminal state — completion, user cancellation, or failure — with the
// same formatting as the message header timestamp. Empty while running or
// when no end timestamp is known.
function runEndTimeLabel(assistantRun) {
  if (assistantRun.status === 'running') {
    return '';
  }
  return formatTime(assistantRun.endTimestamp);
}

function formatRunDuration(assistantRun, nowMs = Date.now()) {
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
  if (start === null) {
    return '';
  }
  if (assistantRun.status === 'running') {
    return formatDurationMs(
      Math.max(0, nowMs - start),
      'chat.runDurationSeconds',
    );
  }
  if (end === null || end < start) {
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
  if (elapsedSeconds < 60) {
    return t(i18nKey, '{seconds}s', {
      seconds: Math.round(elapsedSeconds),
    });
  }
  const totalSeconds = Math.round(elapsedSeconds);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return t('chat.durationHoursMinutes', '{hours}h {minutes}m', {
      hours,
      minutes,
    });
  }
  return t('chat.durationMinutesSeconds', '{minutes}m {seconds}s', {
    minutes,
    seconds,
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

function toolStartedTimestamp(tool) {
  return tool?.timing?.started_at ?? tool?.startedEvent?.timestamp ?? '';
}

function elapsedSinceTimestamp(timestamp, nowMs) {
  const start = timestampToMs(timestamp);
  if (start === null || !Number.isFinite(nowMs)) {
    return null;
  }
  return Math.max(0, nowMs - start);
}

function toolDisplay(tool) {
  const display =
    tool?.display ??
    tool?.toolCall?.display ??
    tool?.resultEvent?.payload?.display ??
    tool?.resultEvent?.payload?.message?.tool_display ??
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

  if (toolName === 'process') {
    const action = trimmedString(args.action);
    if (action) {
      return [action, trimmedString(args.session_id)]
        .filter(Boolean)
        .join(' · ');
    }
    const legacyOperation = isPlainObject(args.request)
      ? trimmedString(args.request.operation)
      : '';
    if (legacyOperation) {
      return legacyOperation;
    }
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
  return '';
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
  if (!isPlainObject(parsedArguments)) {
    return {};
  }
  return isPlainObject(parsedArguments.request)
    ? parsedArguments.request
    : parsedArguments;
}

function subAgentResultEnvelope(tool) {
  const parsedResult = parseJsonValue(tool.result);
  return isPlainObject(parsedResult) ? parsedResult : {};
}

export function subAgentResultData(tool) {
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
  const action = trimmedString(args.action);
  if (action && action !== 'run') {
    return [action, trimmedString(args.id)].filter(Boolean).join(' · ');
  }
  const agentId = trimmedString(args.agent_id);
  const preview = truncateToolLabel(
    trimmedString(args.content),
    MAX_SUBAGENT_PREVIEW_LENGTH,
  );
  return [agentId, preview].filter(Boolean).join(' · ');
}

function externalSubAgentStatus(tool, subAgentStatuses) {
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  // With a known run id only the run-scoped status applies. The session-keyed
  // entry may describe an earlier or later run of the same reused child
  // session, so falling back to it would settle this row with another run's
  // terminal state.
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    return subAgentStatusToDotStatus(
      trimmedString(statuses[`run:${runId}`]).toLowerCase(),
    );
  }

  const queueItemId = subAgentQueueItemId(tool);
  if (
    queueItemId &&
    Object.prototype.hasOwnProperty.call(statuses, `queue:${queueItemId}`)
  ) {
    return subAgentStatusToDotStatus(
      trimmedString(statuses[`queue:${queueItemId}`]).toLowerCase(),
    );
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (!agentId || !sessionId) {
    return '';
  }
  return subAgentStatusToDotStatus(
    trimmedString(statuses[`session:${agentId}::${sessionId}`]).toLowerCase(),
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
