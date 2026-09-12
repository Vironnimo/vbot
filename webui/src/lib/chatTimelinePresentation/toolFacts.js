import { isPlainObject } from '$lib/values.js';
import { t } from '$lib/i18n.js';
import { timestampToMs } from './time.js';

export const toolStatus = (tool) => {
  if (tool.status === 'failed') {
    return 'failed';
  }
  if (tool.status === 'cancelled') {
    return 'cancelled';
  }
  if (tool.status === 'partial') {
    return 'partial';
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
  tool.name || tool.toolCall?.name || t('chat.toolPendingName', 'tool');

export function toolDurationMs(tool) {
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

export function toolStartedTimestamp(tool) {
  return tool?.timing?.started_at ?? tool?.startedEvent?.timestamp ?? '';
}

export function toolDisplay(tool) {
  const display =
    tool?.display ??
    tool?.toolCall?.display ??
    tool?.resultEvent?.payload?.display ??
    tool?.resultEvent?.payload?.message?.tool_display ??
    tool?.startedEvent?.payload?.display;
  return isPlainObject(display) ? display : null;
}

export function toolDisplayFromEvent(event) {
  const display = event?.payload?.display;
  return isPlainObject(display) ? display : null;
}
