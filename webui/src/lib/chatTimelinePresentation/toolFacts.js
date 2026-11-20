import { isPlainObject } from '$lib/values.js';
import { t } from '$lib/i18n.js';
import { timestampToMs } from './time.js';
import { parseJsonValue, trimmedString } from './values.js';

export const toolStatus = (tool) => {
  if (tool.status === 'failed') {
    return 'failed';
  }
  if (tool.status === 'cancelled') {
    return 'cancelled';
  }
  // The Agent's Permission Rules refused the call: the user's limit, not a
  // fault.
  if (tool.status === 'refused') {
    return 'refused';
  }
  if (tool.status === 'interrupted') {
    return 'interrupted';
  }
  if (tool.status === 'partial') {
    return 'partial';
  }
  if (tool.status === 'success' || tool.status === 'completed') {
    return 'success';
  }
  return 'running';
};

const EXECUTION_STATE_TITLES = {
  running: () => t('chat.toolState.running'),
  success: () => t('chat.toolState.success'),
  partial: () => t('chat.toolState.partial'),
  failed: () => t('chat.toolState.failed'),
  cancelled: () => t('chat.toolState.cancelled'),
  refused: () => t('chat.toolState.refused'),
  interrupted: () => t('chat.toolState.interrupted'),
  ended: () => t('chat.toolState.ended'),
};

// The state of a Tool call, Sub-Agent Run or background command in words,
// for the tooltip behind its status dot.
export const executionStateTitle = (status) =>
  (EXECUTION_STATE_TITLES[status] ?? EXECUTION_STATE_TITLES.running)();

// A tool row the model is still *streaming* — the call has been previewed from
// its argument deltas but not dispatched yet (no `tool_call_started`). It shares
// the `running` bucket in `toolStatus` (both are "not settled"), but the dot must
// read differently: a preparing call has not begun executing, so an early
// streamed sibling in a parallel batch should not look like it has been running
// for ages. The server sets it to `running` the moment the call dispatches.
export const isToolPreparing = (tool) => tool?.status === 'preparing';

export const toolArguments = (tool) =>
  tool.arguments ?? tool.toolCall?.arguments ?? streamingPreviewArguments(tool);

// While a tool call is still streaming, the completed top-level string fields
// extracted from the partial arguments JSON stand in for the parsed arguments
// so the preparing row can already show e.g. a write's file path.
export const streamingPreviewArguments = (tool) =>
  isPlainObject(tool?.previewArguments) &&
  Object.keys(tool.previewArguments).length > 0
    ? tool.previewArguments
    : undefined;

export const toolNameForRunTool = (tool) =>
  tool.name || tool.toolCall?.name || t('chat.toolPendingName');

export function toolDurationMs(tool) {
  if (Number.isFinite(tool?.durationMs) && tool.durationMs >= 0) {
    return tool.durationMs;
  }
  const start = timestampToMs(tool?.timing?.started_at ?? tool?.startedAt);
  const end = timestampToMs(tool?.timing?.completed_at ?? tool?.finishedAt);
  if (start === null || end === null || end < start) {
    return null;
  }
  return end - start;
}

export function toolStartedTimestamp(tool) {
  return tool?.timing?.started_at ?? tool?.startedAt ?? '';
}

export function toolDisplay(tool) {
  const display = tool?.display ?? tool?.toolCall?.display;
  return isPlainObject(display) ? display : null;
}

export function toolDisplayFromEvent(event) {
  const display = event?.payload?.display;
  return isPlainObject(display) ? display : null;
}

// The shell's display owns argv quoting and argument dialects. The command
// block keeps the literal text past the primary part's shorter limit.
export function shellCommandPresentation(tool) {
  const display = toolDisplay(tool);
  const primary = Array.isArray(display?.primary) ? display.primary : [];
  const part = primary.find((part) => part?.kind === 'command');
  const title = primary.find((part) => part?.kind === 'description');
  const block = Array.isArray(display?.details)
    ? display.details.find(
        (block) =>
          block?.type === 'text' &&
          block.label === 'command' &&
          typeof block.text === 'string',
      )
    : null;
  const args = parseJsonValue(toolArguments(tool));
  const description = trimmedString(
    title?.full_value || title?.value || args?.description,
  );
  const primaryCommand = part?.full_value || part?.value || title?.detail;
  const argument = typeof args?.command === 'string' ? args.command : '';
  const command =
    block?.text ??
    (typeof primaryCommand === 'string' ? primaryCommand : argument);
  // Older displays only marked a cut with an ellipsis. A standalone event
  // can carry the complete command alongside that same shortened display.
  if (
    command.endsWith('…') &&
    argument.length >= command.length &&
    tool.rawTruncated !== true &&
    argument.startsWith(command.slice(0, -1))
  ) {
    return { command: argument, description, needsRaw: false };
  }
  // The server bounds strings by Unicode code point. A Secret reference can
  // replace part of an already-shortened historical display before it arrives.
  const limit = block ? 16384 : primaryCommand ? 8192 : 1000;
  const legacyCut =
    command.endsWith('…') &&
    (command.includes('{{secret:') || Array.from(command).length === limit);
  const needsRaw =
    block?.truncated === true ||
    legacyCut ||
    (!command && isPlainObject(args) && Object.keys(args).length > 0);
  return { command, description, needsRaw };
}
