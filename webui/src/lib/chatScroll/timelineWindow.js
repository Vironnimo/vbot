// Which Chat timeline rows are mounted, internal to the scroll controller.
// The window covers the viewport plus about one viewport of overscan on each
// side, expressed in row ids so rows added or removed around it do not shift
// it. Held rows stay mounted outside the window. Every run of unmounted rows
// becomes one spacer with the height the layout model assigns it.

const OVERSCAN_VIEWPORTS = 1;
// Hysteresis: the window is recomputed only when its margin beyond the
// viewport drops below KEEP or grows beyond TRIM viewport heights, so small
// scrolls do not churn mounts.
const KEEP_VIEWPORTS = 0.5;
const TRIM_VIEWPORTS = 2.5;
const DEFAULT_VIEWPORT_HEIGHT_PX = 800;

// Spacer keys share the keyed list with row ids; the NUL prefix cannot occur
// in a row id.
const TOP_SPACER_KEY = '\u0000spacer-top';
const BOTTOM_SPACER_KEY = '\u0000spacer-bottom';
const INNER_SPACER_KEY_PREFIX = '\u0000spacer-before-';

export function createTimelineWindow(model) {
  // { mode: 'tail' }                     the rows ending the timeline
  // { mode: 'around', id, delta }        rows around a row whose top sits
  //                                      `delta` px below the viewport top
  // { mode: 'offset', top }              rows around a list offset
  // { mode: 'range', startId, endId,     concrete rows; `endId: null` also
  //   startIndex, endIndex }             mounts every row appended below
  let intent = { mode: 'tail' };
  let viewportHeight = DEFAULT_VIEWPORT_HEIGHT_PX;
  // id -> hold count
  const held = new Map();

  function overscan() {
    return viewportHeight * OVERSCAN_VIEWPORTS;
  }

  function clampIndex(index, size) {
    return Math.max(0, Math.min(index, size - 1));
  }

  function spanAround(top) {
    const size = model.size();
    return [
      model.indexAt(top - overscan()),
      Math.min(size, model.indexAt(top + viewportHeight + overscan()) + 1),
    ];
  }

  // [start, end) row indices of the current intent.
  function resolve() {
    const size = model.size();
    if (size === 0) {
      return [0, 0];
    }
    if (intent.mode === 'range') {
      const start =
        model.indexOf(intent.startId) ?? clampIndex(intent.startIndex, size);
      const end =
        intent.endId === null
          ? size
          : (model.indexOf(intent.endId) ??
              clampIndex(intent.endIndex - 1, size)) + 1;
      return [start, Math.max(start + 1, end)];
    }
    if (intent.mode === 'around') {
      const index = model.indexOf(intent.id);
      if (index !== undefined) {
        return spanAround(model.offsetOf(index) - intent.delta);
      }
    }
    if (intent.mode === 'offset') {
      return spanAround(intent.top);
    }
    return [model.indexAt(model.total() - viewportHeight - overscan()), size];
  }

  function concreteRange(start, end, openEnd) {
    const size = model.size();
    return {
      mode: 'range',
      startId: model.idAt(start),
      startIndex: start,
      endId: openEnd || end >= size ? null : model.idAt(end - 1),
      endIndex: end,
    };
  }

  // Re-derives the window from the viewport (`top` in list coordinates).
  // Returns whether the mounted rows or the spacer heights changed.
  function update(top, height, { followTail = false } = {}) {
    if (height > 0) {
      viewportHeight = height;
    }
    const size = model.size();
    if (size === 0) {
      intent = { mode: 'tail' };
      return false;
    }
    const [start, end] = resolve();
    const bottom = top + viewportHeight;
    const startY = model.offsetOf(start);
    const endY = model.offsetOf(end);
    const keep = viewportHeight * KEEP_VIEWPORTS;
    const trim = viewportHeight * TRIM_VIEWPORTS;
    const covered =
      startY <= Math.max(0, top - keep) &&
      endY >= Math.min(model.total(), bottom + keep);
    const lean = startY >= top - trim && endY <= bottom + trim;
    if (covered && lean && !(followTail && end < size)) {
      // Pin the resolved rows by id so later model changes (estimates,
      // prepended rows) cannot move them. A window reaching the last row
      // stays open, so appended rows mount at once.
      intent = concreteRange(start, end, followTail);
      return false;
    }
    // The window moves, so unmeasured rows adopt the latest estimates now.
    // The viewport top is carried through its row, because new estimates
    // move every offset below the rows they change.
    const topIndex = model.indexAt(top);
    const topWithinRow = top - model.offsetOf(topIndex);
    const estimatesChanged = model.refreshEstimates();
    const viewportTop = model.offsetOf(topIndex) + topWithinRow;
    const [nextStart, nextEnd] = followTail
      ? [model.indexAt(viewportTop - overscan()), size]
      : spanAround(viewportTop);
    intent = concreteRange(nextStart, nextEnd, followTail);
    return estimatesChanged || nextStart !== start || nextEnd !== end;
  }

  function heldIndices(start, end) {
    const before = [];
    const after = [];
    for (const id of held.keys()) {
      const index = model.indexOf(id);
      if (index === undefined) continue;
      if (index < start) before.push(index);
      else if (index >= end) after.push(index);
    }
    before.sort((left, right) => left - right);
    after.sort((left, right) => left - right);
    return { before, after };
  }

  // The keyed render list for `items` (already synced into the model): row
  // entries `{ key, item }` and spacer entries `{ key, spacer, height }`.
  // `layoutKey` changes exactly when mounted rows or spacer heights change.
  function plan(items) {
    const size = items.length;
    if (size === 0) {
      return { entries: [], layoutKey: '' };
    }
    const [start, end] = resolve();
    const { before, after } = heldIndices(start, end);
    const entries = [];
    const layoutParts = [];
    let previous = -1;
    function addSpacer(key, from, to) {
      const height = model.rangeHeight(from, to);
      entries.push({ key, spacer: true, height });
      layoutParts.push(`${Math.round(height)}`);
    }
    function addRow(index) {
      if (previous >= 0 && index > previous + 1) {
        addSpacer(
          `${INNER_SPACER_KEY_PREFIX}${items[index].id}`,
          previous + 1,
          index,
        );
      }
      if (previous < 0) {
        addSpacer(TOP_SPACER_KEY, 0, index);
      }
      entries.push({ key: items[index].id, item: items[index] });
      layoutParts.push(items[index].id);
      previous = index;
    }
    before.forEach(addRow);
    for (let index = start; index < end; index += 1) {
      addRow(index);
    }
    after.forEach(addRow);
    addSpacer(BOTTOM_SPACER_KEY, previous + 1, size);
    return { entries, layoutKey: layoutParts.join('\n') };
  }

  return {
    showTail() {
      intent = { mode: 'tail' };
    },
    showAround(id, delta) {
      intent = { mode: 'around', id, delta };
    },
    showOffset(top) {
      intent = { mode: 'offset', top };
    },
    update,
    plan,
    // Keeps a row mounted outside the window until the returned release runs.
    hold(id) {
      held.set(id, (held.get(id) ?? 0) + 1);
      let released = false;
      return () => {
        if (released) return false;
        released = true;
        const count = (held.get(id) ?? 1) - 1;
        if (count > 0) held.set(id, count);
        else held.delete(id);
        return true;
      };
    },
  };
}
