import { workingSpanStarts } from './chatTimelinePresentation.js';

// The server owns conversation semantics. This module applies its keyed
// projections, preserving unchanged objects so streaming touches only its tail.
// Items are immutable: every change replaces the changed objects up to a new
// `timelineItems` array, which Chat keeps outside its deep `$state` proxy
// (`chatState/timelineItems.svelte.js`) and renders by identity.
export function visibleTimelineItemsForRender(sessionState) {
  return (sessionState?.timelineItems ?? []).flatMap((item) =>
    item.type === 'assistant_run' &&
    ['running', 'completed'].includes(item.status) &&
    item.items?.length > 0 &&
    item.items.every((child) => child.type === 'compaction_separator')
      ? item.items
      : [item],
  );
}

// Parts of each long Run, per Run object and Working mode. Patches replace a
// changed Run and keep unchanged ones, so an unchanged Run keeps its part
// objects and their rows do not render again while another Run streams.
const longRunParts = new WeakMap();

// Run is a semantic unit, not an indivisible DOM row. Stable position buckets
// let the existing viewport unmount old parts of one unusually long Run. In
// compact Work details a Working span stays in its first row's part, so its
// group is never cut apart. Every part carries the whole Run as `runItems`.
export function timelineRowsForViewport(items, workingMode = 'normal') {
  const mode = workingMode === 'compact' ? 'compact' : 'normal';
  return items.flatMap((item) => {
    if (item.type !== 'assistant_run' || item.items.length <= 60) return [item];
    let parts = longRunParts.get(item);
    if (!parts) longRunParts.set(item, (parts = {}));
    return (parts[mode] ??= longRunViewportParts(item, mode));
  });
}

function longRunViewportParts(item, mode) {
  const spanStarts = mode === 'compact' ? workingSpanStarts(item) : new Map();
  const positions = new Map(
    item.items.map((child, index) => [child.id, child.position ?? index]),
  );
  const groups = new Map();
  for (const child of item.items) {
    const anchor = spanStarts.get(child.id) ?? child;
    const part = Math.floor(positions.get(anchor.id) / 30);
    if (!groups.has(part)) groups.set(part, []);
    groups.get(part).push(child);
  }
  const parts = [...groups];
  return parts.map(([part, children], index) => ({
    ...item,
    id: `${item.id}:part:${part}`,
    sourceItemId: item.id,
    runItems: item.items,
    items: children,
    segment_first: index === 0,
    segment_tail: index === parts.length - 1,
  }));
}

function runIdOf(item) {
  return item?.runId ?? item?.run_id ?? item?.message?.history_run_id;
}

function retainWindowPrefix(previous, incoming) {
  if (!previous || !Number.isInteger(incoming?.window_start)) return incoming;
  const incomingIds = new Set((incoming.items ?? []).map((child) => child.id));
  const prefix = (previous.items ?? []).filter(
    (child) =>
      child.type !== 'history_gap' &&
      !incomingIds.has(child.id) &&
      child.position < incoming.window_start,
  );
  if (!prefix.length) return incoming;
  return {
    ...incoming,
    items: [
      ...(previous.items ?? []).filter((child) => child.type === 'history_gap'),
      ...prefix,
      ...(incoming.items ?? []).filter((child) => child.type !== 'history_gap'),
    ],
  };
}

export function mergeRunChildPage(session, page) {
  if (session.historyGeneration !== page.history_generation) return false;
  const run = session.timelineItems.find(
    (item) => item.type === 'assistant_run' && runIdOf(item) === page.run_id,
  );
  // The page continues exactly the gap it was read for; once the Run was
  // reset or extended past it, merging would leave a hole or duplicates.
  const gap = run?.items.find((child) => child.type === 'history_gap');
  if (!gap || gap.before !== page.before) return false;
  const children = new Map(
    run.items
      .filter((child) => child.type !== 'history_gap')
      .map((child) => [child.id, child]),
  );
  for (const child of page.items ?? [])
    if (!children.has(child.id)) children.set(child.id, child);
  const updated = withCollections({
    ...run,
    items: [...children.values()].sort(
      (left, right) => left.position - right.position,
    ),
  });
  session.timelineItems = session.timelineItems.map((item) =>
    item === run ? updated : item,
  );
  return true;
}

function withCollections(item) {
  if (item?.type !== 'assistant_run') return item;
  const children = item.items ?? [];
  return {
    ...item,
    items: children,
    tools: children.filter((child) => child.type === 'tool_call'),
    outputs: children.filter((child) => child.type === 'assistant_output'),
    reasoning: children.filter((child) => child.type === 'reasoning'),
  };
}

// A snapshot may repeat a long unchanged prefix after reconnection. Reuse its
// JSON subtrees, including child identities held by disclosure/media controls.
function reuse(previous, incoming) {
  if (previous === incoming) return previous;
  if (
    !previous ||
    !incoming ||
    typeof previous !== 'object' ||
    typeof incoming !== 'object'
  )
    return incoming;
  if (Array.isArray(previous) !== Array.isArray(incoming)) return incoming;
  const keys = Object.keys(incoming);
  let same = keys.length === Object.keys(previous).length;
  const result = Array.isArray(incoming) ? [] : {};
  for (const key of keys) {
    result[key] = reuse(previous[key], incoming[key]);
    same &&= result[key] === previous[key];
  }
  return same ? previous : result;
}

export function mergeTimelineItems(
  existing,
  incoming,
  { prepend = false } = {},
) {
  const known = new Map((existing ?? []).map((item) => [item.id, item]));
  const updates = new Map(
    (incoming ?? []).map((item) => [
      item.id,
      reuse(
        known.get(item.id),
        withCollections(retainWindowPrefix(known.get(item.id), item)),
      ),
    ]),
  );
  const retained = (existing ?? []).map((item) => updates.get(item.id) ?? item);
  // A page carries its own ordering, including new rows before existing ones.
  let nextId = null;
  for (const item of [...updates.values()].reverse()) {
    if (!known.has(item.id)) {
      const anchor =
        nextId === null ? -1 : retained.findIndex((row) => row.id === nextId);
      retained.splice(
        anchor < 0 ? (prepend ? 0 : retained.length) : anchor,
        0,
        item,
      );
    }
    nextId = item.id;
  }
  return retained;
}

export function loadTimelineItems(sessionState, items, options = {}) {
  if (!Array.isArray(items)) return;
  const previous = sessionState.timelineItems ?? [];
  const activeRunId = options.activeRunId;
  const newerLive =
    activeRunId &&
    (sessionState.timelineSequences?.[activeRunId] ?? -1) >
      (options.timelineSequence ?? -1);
  const incoming = newerLive
    ? items.filter((item) => runIdOf(item) !== activeRunId)
    : items;
  if (options.incremental || options.prepend) {
    const replaced = new Set(
      (options.replaceRunIds ?? []).filter(
        (runId) => !newerLive || runId !== activeRunId,
      ),
    );
    const incomingIds = new Set(incoming.map((item) => item.id));
    sessionState.timelineItems = mergeTimelineItems(
      previous.filter(
        (item) => !replaced.has(runIdOf(item)) || incomingIds.has(item.id),
      ),
      incoming,
      options,
    );
  } else {
    const known = new Map(
      (options.reset ? [] : previous).map((item) => [item.id, item]),
    );
    sessionState.timelineItems = [
      ...incoming.map((item) =>
        reuse(
          known.get(item.id),
          withCollections(retainWindowPrefix(known.get(item.id), item)),
        ),
      ),
      ...(newerLive
        ? previous.filter((item) => runIdOf(item) === activeRunId)
        : []),
    ];
  }
  if (options.reset) sessionState.timelineSequences = {};
  if (activeRunId && Number.isInteger(options.timelineSequence) && !newerLive) {
    (sessionState.timelineSequences ??= {})[activeRunId] =
      options.timelineSequence;
  }
}

export function applyTimelineEvent(sessionState, type, data) {
  if (!data?.run_id || !Number.isInteger(data.sequence)) return false;
  const runId = data.run_id;
  const sequences = (sessionState.timelineSequences ??= {});
  if (data.sequence < (sequences[runId] ?? -1)) return false;
  const previous = sessionState.timelineItems ?? [];
  if (type === 'timeline_snapshot') {
    if (!Array.isArray(data.items)) return false;
    const known = new Map(previous.map((item) => [item.id, item]));
    const replacement = data.items.map((item) =>
      reuse(
        known.get(item.id),
        withCollections(retainWindowPrefix(known.get(item.id), item)),
      ),
    );
    sessionState.timelineItems = replaceRunItems(previous, runId, replacement);
  } else if (type === 'timeline_update') {
    if (data.sequence === sequences[runId]) return false;
    if (data.base_sequence !== sequences[runId]) return false;
    const removedItems = new Set(data.remove ?? []);
    let next = mergeTimelineItems(
      previous.filter((item) => !removedItems.has(item.id)),
      data.upsert ?? [],
    );
    if (data.run) {
      const oldRun = next.find((item) => item.id === data.run.id);
      // A patch cannot establish a missing base; reconnect for a snapshot.
      if (!oldRun) return false;
      const removed = new Set(data.remove_children ?? []);
      const children = new Map(
        (oldRun.items ?? [])
          .filter((child) => !removed.has(child.id))
          .map((child) => [child.id, child]),
      );
      for (const child of data.children ?? []) children.set(child.id, child);
      for (const [id, position] of Object.entries(data.positions ?? {})) {
        if (children.has(id))
          children.set(id, { ...children.get(id), position });
      }
      for (const update of data.child_updates ?? []) {
        const previousChild = children.get(update.id);
        if (!previousChild) return false;
        const child = { ...previousChild };
        for (const [key, suffix] of Object.entries(update.append ?? {}))
          child[key] = `${child[key] ?? ''}${suffix}`;
        Object.assign(child, update.set ?? {});
        children.set(update.id, child);
      }
      const order = data.windowed
        ? [...children.keys()].sort(
            (left, right) =>
              children.get(left).position - children.get(right).position,
          )
        : (data.order ?? [...children.keys()]);
      const updated = withCollections({
        ...oldRun,
        ...data.run,
        items: order.flatMap((id) =>
          children.has(id) ? [children.get(id)] : [],
        ),
      });
      next = next.map((item) => (item.id === updated.id ? updated : item));
    }
    if (data.item_order) {
      const byId = new Map(next.map((item) => [item.id, item]));
      if (data.item_order.some((id) => !byId.has(id))) return false;
      next = replaceRunItems(
        next,
        runId,
        data.item_order.map((id) => byId.get(id)),
      );
    }
    sessionState.timelineItems = next;
  } else return false;
  sequences[runId] = data.sequence;
  return true;
}

function replaceRunItems(items, runId, replacement) {
  const replacementIds = new Set(replacement.map((item) => item.id));
  const owned = (item) =>
    runIdOf(item) === runId || replacementIds.has(item.id);
  const index = items.findIndex(owned);
  const position = index < 0 ? items.length : index;
  return [
    ...items.slice(0, position).filter((item) => !owned(item)),
    ...replacement,
    ...items.slice(position).filter((item) => !owned(item)),
  ];
}
