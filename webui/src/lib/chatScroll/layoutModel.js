// Layout model of the Chat timeline rows, internal to the scroll controller.
// It keeps the ordered row ids with one height each: the measured border-box
// height of a row that was mounted, or an estimate for a row that has not been
// measured yet (the running average of measured rows of the same kind, or a
// per-kind default before any measurement). Offsets are prefix sums over
// those heights, so the unmounted parts of the timeline can be represented
// by spacers of exactly the height the model assigns them.
//
// Estimates follow the running averages only when `refreshEstimates()` runs
// (the window calls it when it moves). Measuring one row therefore never
// resizes the spacers standing in for other rows, so a streaming row or an
// image load below the reading position cannot shift the content above it.

const DEFAULT_ESTIMATES = new Map([
  ['assistant_run', 240],
  ['message:user', 90],
  ['message:assistant', 160],
  ['message:error', 90],
  ['event', 70],
  ['compaction_separator', 44],
  ['takeover_separator', 44],
]);
const FALLBACK_ESTIMATE = 120;
// Sub-pixel noise from fractional layout must not count as a change.
const HEIGHT_EPSILON_PX = 0.5;

export function timelineItemKind(item) {
  if (item?.type === 'message') {
    return `message:${item.message?.role ?? ''}`;
  }
  return item?.type ?? '';
}

export function createLayoutModel() {
  let sourceItems = null;
  let ids = [];
  let kinds = [];
  let indexById = new Map();
  // id -> { height, kind }; only ids present in the current list.
  const measured = new Map();
  // kind -> { sum, count } over `measured`.
  const kindStats = new Map();
  // kind -> estimate in use for unmeasured rows; see refreshEstimates().
  const estimates = new Map();
  let offsets = new Float64Array(1);
  let offsetsDirty = true;

  function addStat(kind, height) {
    const stat = kindStats.get(kind) ?? { sum: 0, count: 0 };
    stat.sum += height;
    stat.count += 1;
    kindStats.set(kind, stat);
  }

  function removeStat(kind, height) {
    const stat = kindStats.get(kind);
    if (!stat) {
      return;
    }
    stat.sum -= height;
    stat.count -= 1;
    if (stat.count <= 0) {
      kindStats.delete(kind);
    }
  }

  function estimate(kind) {
    return (
      estimates.get(kind) ?? DEFAULT_ESTIMATES.get(kind) ?? FALLBACK_ESTIMATE
    );
  }

  // Adopts the running averages as estimates; returns whether any estimate
  // changed by a pixel or more.
  function refreshEstimates() {
    let changed = false;
    for (const [kind, stat] of kindStats) {
      const average = stat.sum / stat.count;
      if (Math.abs(average - estimate(kind)) >= 1) {
        estimates.set(kind, average);
        changed = true;
      }
    }
    if (changed) {
      offsetsDirty = true;
    }
    return changed;
  }

  function heightAt(index) {
    return measured.get(ids[index])?.height ?? estimate(kinds[index]);
  }

  function ensureOffsets() {
    if (!offsetsDirty) {
      return;
    }
    if (offsets.length !== ids.length + 1) {
      offsets = new Float64Array(ids.length + 1);
    }
    let sum = 0;
    for (let index = 0; index < ids.length; index += 1) {
      offsets[index] = sum;
      sum += heightAt(index);
    }
    offsets[ids.length] = sum;
    offsetsDirty = false;
  }

  // Adopts the current row list. Streaming flushes hand in a new array with
  // the same ids most of the time; only a changed id sequence rebuilds the
  // index, and measurements of rows that left the list are dropped.
  function sync(items) {
    if (items === sourceItems) {
      return;
    }
    sourceItems = items;
    let sameIds = items.length === ids.length;
    for (let index = 0; sameIds && index < items.length; index += 1) {
      sameIds = items[index].id === ids[index];
    }
    if (sameIds) {
      return;
    }
    ids = items.map((item) => item.id);
    kinds = items.map(timelineItemKind);
    indexById = new Map(ids.map((id, index) => [id, index]));
    for (const [id, entry] of measured) {
      if (!indexById.has(id)) {
        measured.delete(id);
        removeStat(entry.kind, entry.height);
      }
    }
    offsetsDirty = true;
  }

  // Records a mounted row's measured height; returns whether it changed.
  function setMeasured(id, height) {
    const index = indexById.get(id);
    if (index === undefined || !Number.isFinite(height) || height < 0) {
      return false;
    }
    const kind = kinds[index];
    const previous = measured.get(id);
    if (
      previous &&
      previous.kind === kind &&
      Math.abs(previous.height - height) < HEIGHT_EPSILON_PX
    ) {
      return false;
    }
    if (previous) {
      removeStat(previous.kind, previous.height);
    }
    measured.set(id, { height, kind });
    addStat(kind, height);
    offsetsDirty = true;
    return true;
  }

  // Largest row index whose top offset is at or above `y`, clamped to the
  // list.
  function indexAt(y) {
    ensureOffsets();
    if (ids.length === 0) {
      return 0;
    }
    let low = 0;
    let high = ids.length - 1;
    while (low < high) {
      const middle = (low + high + 1) >> 1;
      if (offsets[middle] <= y) {
        low = middle;
      } else {
        high = middle - 1;
      }
    }
    return low;
  }

  function offsetOf(index) {
    ensureOffsets();
    return offsets[Math.max(0, Math.min(index, ids.length))];
  }

  return {
    sync,
    setMeasured,
    refreshEstimates,
    indexAt,
    offsetOf,
    // Height of rows [from, to).
    rangeHeight: (from, to) => (to > from ? offsetOf(to) - offsetOf(from) : 0),
    total: () => offsetOf(ids.length),
    indexOf: (id) => indexById.get(id),
    idAt: (index) => ids[index],
    size: () => ids.length,
  };
}
