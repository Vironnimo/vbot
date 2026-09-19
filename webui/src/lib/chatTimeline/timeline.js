import { t } from '../i18n.js';
import { stripTimelineSequence, normalizedIterationCount } from './model.js';
import { historyTimelineItems } from './history.js';
import { reconcileTimeline } from './reconciliation.js';
import { liveTimelineItems } from './live.js';

export function visibleTimelineItemsForRender(sessionState) {
  if (!sessionState) {
    return [];
  }

  return buildVisibleTimelineItems(sessionState, [
    ...(sessionState.runEvents ?? []),
    ...(sessionState.streamingRunEvents ?? []),
  ]);
}

export function assistantRunChildProgressKey(child) {
  if (!child || typeof child !== 'object') {
    return '0::0';
  }

  const { chunkCount, latestSequence } = childStreamingProgress(child);
  if (child.type === 'tool_call') {
    const toolNameLength = (child.name ?? '').length;
    const streamedArgumentsLength = (child.partialArgumentsText ?? '').length;
    const finalizedArgumentsLength =
      typeof child.arguments === 'string' ? child.arguments.length : 0;
    const outputLength =
      (child.stdout ?? '').length + (child.stderr ?? '').length;
    return `${chunkCount}:${latestSequence ?? ''}:${toolNameLength}:${streamedArgumentsLength + finalizedArgumentsLength}:${outputLength}:${child.resultEvent ? 1 : 0}`;
  }

  const contentLength =
    typeof child.content === 'string' ? child.content.length : 0;
  if (child.type === 'compaction_separator') {
    const summaryLength =
      typeof child.message?.content === 'string'
        ? child.message.content.length
        : 0;
    return `${chunkCount}:${latestSequence ?? ''}:${child.status ?? ''}:${child.contextTokensBefore ?? ''}:${child.contextTokensAfter ?? ''}:${summaryLength}`;
  }
  return `${chunkCount}:${latestSequence ?? ''}:${contentLength}`;
}

// Per-session memo of projected assistant_run items, keyed by run. A run
// whose group contains a terminal event and no retained streaming delta can no
// longer change: non-delta run events are appended exactly once
// (appendRunEvent dedups by run_id + sequence) and never mutated. A terminal
// Run may temporarily retain deltas while canonical output is still in flight,
// so that group stays uncached until History confirms their persistence.
// Reusing every other terminal Run's projection across the ≤33 ms streaming
// flushes keeps the per-flush rebuild cost bound to the active Run instead of
// growing with Session age (handoff3 B10).
const liveRunProjectionCachesBySession = new WeakMap();

function liveRunProjectionCache(sessionState) {
  let cache = liveRunProjectionCachesBySession.get(sessionState);
  if (!cache) {
    cache = new Map();
    liveRunProjectionCachesBySession.set(sessionState, cache);
  }
  return cache;
}

function buildVisibleTimelineItems(sessionState, runEvents) {
  if (!sessionState) {
    return [];
  }

  const liveItems = liveTimelineItems(
    runEvents,
    liveRunProjectionCache(sessionState),
  );
  applyCurrentRunIterationCount(liveItems, sessionState.currentRun);
  const reconciledItems =
    runEvents.length > 0
      ? reconcileTimeline(sessionState, liveItems, runEvents)
      : historyTimelineItems(sessionState.messages);

  const persistedMessageIds = new Set(
    reconciledItems
      .filter((item) => item.type === 'message' && !item.liveErrorRunId)
      .map((item) => item.message?.id ?? item.id),
  );
  const visibleItems = reconciledItems.filter(
    (item) =>
      !item.liveErrorRunId ||
      !persistedMessageIds.has(item.message?.id ?? item.id),
  );
  return visibleItems.flatMap((item) =>
    isCompactionOnlyRunItem(item)
      ? item.items.map(stripTimelineSequence)
      : [
          stripTimelineSequence(item),
          ...runFailureFallback(item, runEvents, sessionState.messages),
        ],
  );
}

// Some failures (including early executor failures) have no persisted error
// Message. Keep the terminal cause in the timeline until canonical History or
// an error-message event supplies the Run's durable presentation.
function runFailureFallback(item, runEvents, messages) {
  if (item.type !== 'assistant_run' || item.status !== 'failed') return [];
  const runId = item.runId;
  const failure = runEvents.find(
    (event) => event.run_id === runId && event.type === 'run_failed',
  );
  if (
    !failure ||
    runEvents.some(
      (event) =>
        event.run_id === runId &&
        event.type === 'error_message_persisted' &&
        event.payload?.message,
    ) ||
    (messages ?? []).some(
      (message) => message.role === 'run_summary' && message.run_id === runId,
    )
  )
    return [];
  const id = `run-failure-${runId}`;
  return [
    {
      id,
      type: 'message',
      message: {
        id,
        role: 'error',
        content: failure.payload?.error || t('chat.runError', 'Run failed.'),
        timestamp: failure.timestamp,
      },
    },
  ];
}

// A standalone Compaction Run emits nothing but Compaction events, so its run
// block would wrap the exact separator the automatic in-run Compaction renders
// bare. Dissolving it at the render boundary makes both triggers look
// identical. A failed or cancelled run keeps its block and terminal status.
// Failed attempts retain their warning separator; harmless skipped attempts
// remove the placeholder. An in-run auto Compaction never qualifies because its Run
// carries reasoning/output/tool children besides the separator.
function isCompactionOnlyRunItem(item) {
  return (
    item?.type === 'assistant_run' &&
    (item.status === 'running' || item.status === 'completed') &&
    (item.items ?? []).length > 0 &&
    item.items.every((child) => child?.type === 'compaction_separator')
  );
}

function applyCurrentRunIterationCount(liveItems, currentRun) {
  const iterationCount = normalizedIterationCount(currentRun?.iterationCount);
  if (iterationCount === null || !currentRun?.runId) {
    return;
  }
  const assistantRun = (liveItems ?? []).find(
    (item) => item?.type === 'assistant_run' && item.runId === currentRun.runId,
  );
  if (assistantRun) {
    assistantRun.startTimestamp =
      currentRun.startedAt ?? assistantRun.startTimestamp;
    assistantRun.iterationCount = iterationCount;
  }
}

function childStreamingProgress(child) {
  let chunkCount = 0;
  let latestSequence = Number.isFinite(child?.sequence) ? child.sequence : null;

  for (const event of child?.events ?? []) {
    chunkCount += streamEventChunkCount(event);
    const eventLatestSequence = streamEventLatestSequence(event);
    if (!Number.isFinite(eventLatestSequence)) {
      continue;
    }
    latestSequence = Number.isFinite(latestSequence)
      ? Math.max(latestSequence, eventLatestSequence)
      : eventLatestSequence;
  }

  if (chunkCount === 0) {
    chunkCount = (child?.messages ?? []).length;
  }

  return { chunkCount, latestSequence };
}

function streamEventChunkCount(event) {
  if (
    Number.isFinite(event?._streamChunkCount) &&
    event._streamChunkCount > 0
  ) {
    return event._streamChunkCount;
  }
  return 1;
}

function streamEventLatestSequence(event) {
  if (Number.isFinite(event?._streamLatestSequence)) {
    return event._streamLatestSequence;
  }
  return event?.sequence;
}
