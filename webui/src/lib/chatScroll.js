// Single owner of the Chat timeline's scroll behavior. One instance wraps one
// scroll container and manages per-Session viewports with two rules only:
//
// 1. PINNED (stick to bottom): every content growth snaps to the bottom.
// 2. READING: the viewport owns a position; content appended below never moves
//    it, and growth above it (image loads, prepended history, collapsed rows)
//    is corrected once through a content anchor.
//
// Ownership handover is positional, never timer-based: every programmatic
// scroll goes through `writeScroll`, which records the expected scrollTop. A
// scroll event matching that expectation is our own echo; anything else is
// real user motion and immediately reclassifies the viewport from the live
// position. Upward input (wheel/touch/keys) releases the pin before the
// browser scrolls, so streaming growth can never yank the view back down.
//
// The controller also decides which rows are mounted. Once the timeline has
// shown real layout, only the rows in and around the viewport (the window)
// plus held rows are mounted; spacers sized from a per-Session layout model
// of measured and estimated row heights stand in for the rest. The renderer
// asks `renderPlan()` for the rows and spacers to mount and calls
// `rendered()` after every update. Rows mounted above the reading position
// correct it once through the same content anchor, before the browser paints.
// Without layout (hidden, or a DOM without layout) every row stays mounted.
//
// A Run loaded only with its latest steps marks the omitted steps with a
// history gap element (`data-history-gap` = Run id, `data-history-gap-before`
// = cursor). The controller loads them, without any user action, once the gap
// comes within one viewport height of the visible area; the loaded steps grow
// above the reading position like older History.

import { trackInteractionHolds } from './chatScroll/interactionHolds.js';
import { createLayoutModel } from './chatScroll/layoutModel.js';
import { createTimelineWindow } from './chatScroll/timelineWindow.js';
import {
  captureReadingAnchor,
  readingAnchorTop,
} from './chatScroll/readingAnchor.js';

const STICK_TO_BOTTOM_THRESHOLD_PX = 56;
const LOAD_OLDER_THRESHOLD_PX = 48;
const PROGRAMMATIC_ECHO_TOLERANCE_PX = 1;
const MAX_TRACKED_SESSIONS = 100;
const MAX_TRACKED_LAYOUTS = 20;
// Window updates that mount more rows run synchronously after an update so
// the browser never paints a gap; this bounds them per frame, further ones
// wait for the next frame.
const MAX_SYNC_WINDOW_UPDATES_PER_FRAME = 8;

function requestFrame(callback) {
  if (typeof requestAnimationFrame === 'function') {
    return requestAnimationFrame(callback);
  }
  return setTimeout(callback, 16);
}

function cancelFrame(frame) {
  if (typeof cancelAnimationFrame === 'function') {
    cancelAnimationFrame(frame);
  } else {
    clearTimeout(frame);
  }
}

export function createChatScrollController(
  container,
  {
    // The element whose children are the rendered rows and spacers.
    content = null,
    onViewChanged = () => {},
    // The rows to mount changed: the renderer must ask `renderPlan()` again.
    onWindowChanged = () => {},
    shouldLoadOlder = () => false,
    requestLoadOlder = async () => false,
    // Loads the earlier steps of one Run: `{ runId, before }` from its history
    // gap; resolves true once they were merged.
    requestLoadRunHistory = async () => false,
  } = {},
) {
  // sessionId -> { pinned, anchorId, anchorDelta, fallbackTop,
  //                fallbackScrollHeight }
  const viewports = new Map();
  let currentSessionId = '';
  let restorePending = false;
  let restoreToPinned = true;
  let expectedScrollTop = 0;
  // Position at upward input, until the browser moves (null otherwise).
  // This also protects an existing reading position: a queued resize/render
  // correction must not undo motion whose scroll event has not arrived yet.
  let pendingUserScrollTop = null;
  // Each Session owns its pending page; a slow request must not block a
  // different Session after navigation.
  const loadingOlderSessions = new Set();
  // Run history gaps of the current Session requested so far, by Run id and
  // cursor: 'pending', 'loaded' until the merged steps rendered, or
  // 'failed' until real user motion, so a failing load does not retry on
  // every render. A gap that shows again after it loaded (a reset History)
  // loads again.
  let runGapLoads = new Map();
  let contentSyncQueued = false;
  let contentSyncFrame = null;

  // sessionId -> { model, window }, least recently displayed first. Row ids
  // repeat across Sessions, so each Session has its own layout.
  const layouts = new Map();
  let layout = layoutFor('');
  // Set once the content column has shown real layout; stays set, so a
  // timeline that loses its layout (hidden) keeps its last window.
  let virtualActive = false;
  // layoutKey of the latest plan, and of the plan the last correction ran
  // against.
  let plannedLayoutKey = null;
  let correctedLayoutKey = null;
  // Render entries by key from the latest plan, reused while unchanged so
  // the keyed list does not update rows whose item did not change.
  let planEntries = new Map();
  // Mounted row element -> { id, model }.
  const observedRows = new Map();
  let windowSyncFrame = null;
  let syncWindowUpdates = 0;
  let publishQueued = false;
  let destroyed = false;

  const resizeObserver =
    typeof ResizeObserver === 'function'
      ? new ResizeObserver(handleResize)
      : null;
  resizeObserver?.observe(container);
  if (content) {
    resizeObserver?.observe(content);
  }
  const interactionHolds = content
    ? trackInteractionHolds(container, content, hold)
    : null;

  container.addEventListener('scroll', handleContainerScroll);
  // A timeline created visible plans its first rows as a window already.
  virtualActive = hasLayout() && contentHasLayout();

  function createViewport() {
    return {
      pinned: true,
      anchorId: '',
      anchorDelta: 0,
      readingAnchor: null,
      fallbackTop: 0,
      fallbackScrollHeight: 0,
    };
  }

  function viewportFor(sessionId) {
    if (!sessionId) {
      return createViewport();
    }
    let viewport = viewports.get(sessionId);
    if (!viewport) {
      viewport = createViewport();
      viewports.set(sessionId, viewport);
      trimViewports();
    }
    return viewport;
  }

  function trimViewports() {
    while (viewports.size > MAX_TRACKED_SESSIONS) {
      const oldestId = viewports.keys().next().value;
      viewports.delete(oldestId);
    }
  }

  function layoutFor(sessionId) {
    let entry = layouts.get(sessionId);
    if (entry) {
      layouts.delete(sessionId);
    } else {
      const model = createLayoutModel();
      entry = { model, window: createTimelineWindow(model) };
    }
    layouts.set(sessionId, entry);
    while (layouts.size > MAX_TRACKED_LAYOUTS) {
      layouts.delete(layouts.keys().next().value);
    }
    return entry;
  }

  function hasLayout() {
    return container.clientHeight > 0;
  }

  function isNearBottom() {
    return (
      container.offsetHeight + container.scrollTop >
      container.scrollHeight - STICK_TO_BOTTOM_THRESHOLD_PX
    );
  }

  // The single writer for every programmatic scroll position. The browser
  // clamps on assignment; reading back records the position that actually
  // took effect so echo events can be recognized.
  function writeScroll(top) {
    container.scrollTop = top;
    expectedScrollTop = container.scrollTop;
    pendingUserScrollTop = null;
  }

  function writeBottom() {
    // Only snap when content actually fills the viewport; otherwise
    // scrollTop = scrollHeight collapses to the top during initial loads.
    if (container.scrollHeight > container.offsetHeight) {
      writeScroll(container.scrollHeight);
    }
  }

  function handleContainerScroll() {
    if (virtualActive) {
      queueWindowSync();
    }
    if (restorePending) {
      // Transition noise (clamps, programmatic resets) while a restore has
      // not been applied yet; the restore decides the position.
      expectedScrollTop = container.scrollTop;
      return;
    }
    const actual = container.scrollTop;
    if (
      Math.abs(actual - expectedScrollTop) <= PROGRAMMATIC_ECHO_TOLERANCE_PX
    ) {
      // An earlier correction's echo can arrive after new upward input.
      // It must not end the handover before that gesture actually moves.
      return;
    }
    classifyUserScroll(actual);
  }

  function classifyUserScroll(actual) {
    expectedScrollTop = actual;
    pendingUserScrollTop = null;
    const viewport = viewportFor(currentSessionId);
    if (isNearBottom()) {
      viewport.pinned = true;
      viewport.anchorId = '';
    } else {
      viewport.pinned = false;
      captureAnchor(viewport);
      maybeRequestLoadOlder();
    }
    forgetRunGapLoads('failed');
    maybeLoadRunGaps();
    onViewChanged();
  }

  function maybeRequestLoadOlder() {
    if (
      destroyed ||
      loadingOlderSessions.has(currentSessionId) ||
      container.scrollTop > LOAD_OLDER_THRESHOLD_PX ||
      !shouldLoadOlder()
    ) {
      return;
    }
    const requestedSessionId = currentSessionId;
    const previousScrollHeight = container.scrollHeight;
    loadingOlderSessions.add(requestedSessionId);
    Promise.resolve(requestLoadOlder())
      .catch(() => {})
      .finally(() => {
        loadingOlderSessions.delete(requestedSessionId);
        if (destroyed || currentSessionId !== requestedSessionId) {
          return;
        }
        // Prepended history grew the content above the reading position.
        // The anchor correction handles this when an anchor exists; without
        // one (layout-less or replaced content), shift the held pixel
        // position by the exact growth so the reading position survives.
        // With a window, the prepended rows enter as a spacer above the
        // mounted anchor, which the correction after rendering covers.
        const growth = container.scrollHeight - previousScrollHeight;
        if (growth > 0 && !virtualActive) {
          const viewport = viewportFor(currentSessionId);
          if (!viewport.pinned) {
            expectedScrollTop += growth;
            viewport.fallbackTop += growth;
            viewport.fallbackScrollHeight = container.scrollHeight;
          }
        }
        queueContentChanged();
      });
  }

  function maybeLoadRunGaps() {
    if (destroyed || restorePending || !hasLayout()) {
      return;
    }
    const elements = Array.from(
      container.querySelectorAll('[data-history-gap]') ?? [],
    );
    if (elements.length === 0) {
      return;
    }
    const containerRect = container.getBoundingClientRect();
    const reach = container.clientHeight;
    const top = containerRect.top - reach;
    const bottom =
      (containerRect.bottom ?? containerRect.top + container.offsetHeight) +
      reach;
    for (const element of elements) {
      const runId = element.dataset?.historyGap;
      const before = element.dataset?.historyGapBefore ?? '';
      const key = `${runId}\n${before}`;
      if (!runId || runGapLoads.has(key) || !elementHasLayout(element)) {
        continue;
      }
      const rect = element.getBoundingClientRect();
      if (rect.bottom < top || rect.top > bottom) {
        continue;
      }
      const loads = runGapLoads;
      loads.set(key, 'pending');
      Promise.resolve(requestLoadRunHistory({ runId, before }))
        .catch(() => false)
        .then((loaded) => {
          if (destroyed || loads !== runGapLoads) {
            return;
          }
          loads.set(key, loaded ? 'loaded' : 'failed');
          // The merged steps render next; `rendered()` checks the gap they
          // leave behind.
          queueContentChanged();
        });
    }
  }

  function forgetRunGapLoads(outcome) {
    for (const [key, state] of runGapLoads) {
      if (state === outcome) {
        runGapLoads.delete(key);
      }
    }
  }

  function timelineItemElements() {
    return Array.from(
      container.querySelectorAll('[data-timeline-item-id]') ?? [],
    );
  }

  function elementHasLayout(element) {
    return (
      element.getBoundingClientRect().height > 0 || element.offsetHeight > 0
    );
  }

  // Record which content element sits at the current reading position, as an
  // offset the correction below can re-apply after content above it resizes.
  // Only a visible row qualifies: a viewport showing a spacer has none.
  function captureAnchor(viewport) {
    viewport.fallbackTop = container.scrollTop;
    viewport.fallbackScrollHeight = container.scrollHeight;
    const containerRect = container.getBoundingClientRect();
    const containerBottom =
      containerRect.bottom ?? containerRect.top + container.offsetHeight;
    const visible = timelineItemElements().filter((element) => {
      if (!elementHasLayout(element)) {
        return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.bottom > containerRect.top && rect.top < containerBottom;
    });
    // A row holding a Run history gap is where that Run's earlier steps
    // arrive; anchoring to it would push the content being read down.
    const anchor =
      visible.find(
        (element) => !element.querySelector?.('[data-history-gap]'),
      ) ?? visible[0];
    if (!anchor) {
      viewport.anchorId = '';
      viewport.anchorDelta = 0;
      viewport.readingAnchor = null;
      return;
    }
    viewport.anchorId = anchor.dataset.timelineItemId ?? '';
    viewport.anchorDelta = anchor.offsetTop - container.scrollTop;
    viewport.readingAnchor = captureReadingAnchor(anchor, {
      ...containerRect,
      top: containerRect.top,
      bottom: containerBottom,
    });
  }

  // Reading-mode stabilization: derive the position the viewport should hold
  // after the latest content change. The content anchor wins when it still
  // exists; otherwise the last known-good pixel position is held.
  function findAnchorTop(viewport) {
    if (!viewport.anchorId) {
      return null;
    }
    const anchor = timelineItemElements().find(
      (element) =>
        element.dataset.timelineItemId === viewport.anchorId &&
        elementHasLayout(element),
    );
    if (!anchor) {
      return null;
    }
    const readingTop = readingAnchorTop(anchor, viewport.readingAnchor);
    if (readingTop !== null) {
      const top = Math.max(
        0,
        container.scrollTop +
          readingTop -
          container.getBoundingClientRect().top,
      );
      // Keep window aiming and the row fallback aligned with corrections
      // inside the row, including when that paragraph later disappears.
      viewport.anchorDelta = anchor.offsetTop - top;
      return top;
    }
    return Math.max(0, anchor.offsetTop - viewport.anchorDelta);
  }

  // Called (coalesced) after any content growth: streaming deltas, image
  // loads, history pages, session swaps.
  function contentChanged() {
    settlePosition();
    syncWindow();
    maybeLoadRunGaps();
  }

  // Re-applies the viewport's mode to the current content: a pending restore,
  // following the bottom, or holding the reading anchor.
  function settlePosition() {
    if (restorePending) {
      applyRestore();
      return;
    }
    const viewport = viewportFor(currentSessionId);
    if (pendingUserScrollTop !== null) {
      if (
        Math.abs(container.scrollTop - pendingUserScrollTop) >
        PROGRAMMATIC_ECHO_TOLERANCE_PX
      ) {
        // Read native motion now, before a queued correction or window
        // update can put back the old anchor. Its later scroll event is
        // just an echo of this already accepted position.
        classifyUserScroll(container.scrollTop);
      } else {
        // An ineffective gesture at the bottom must not stop following.
        if (isNearBottom()) {
          viewport.pinned = true;
          pendingUserScrollTop = null;
          onViewChanged();
        }
        return;
      }
    }
    if (viewport.pinned) {
      writeBottom();
      return;
    }
    const desired = findAnchorTop(viewport) ?? expectedScrollTop;
    if (
      Math.abs(desired - container.scrollTop) > PROGRAMMATIC_ECHO_TOLERANCE_PX
    ) {
      writeScroll(desired);
    }
  }

  function queueContentChanged() {
    if (destroyed) {
      return;
    }
    if (typeof requestAnimationFrame !== 'function') {
      contentChanged();
      return;
    }
    if (contentSyncQueued) {
      return;
    }
    contentSyncQueued = true;
    contentSyncFrame = requestAnimationFrame(() => {
      contentSyncQueued = false;
      contentSyncFrame = null;
      contentChanged();
    });
  }

  // Save the outgoing session's viewport, then prepare the incoming one. Must
  // run before the DOM swap so anchors are captured against the old content.
  function sessionChanged(sessionId) {
    saveViewport(currentSessionId);
    currentSessionId = sessionId || '';
    layout = layoutFor(currentSessionId);
    interactionHolds?.releaseAll();
    const viewport = viewportFor(currentSessionId);
    runGapLoads = new Map();
    restorePending = true;
    restoreToPinned = viewport.pinned;
    pendingUserScrollTop = null;
    aimWindow(viewport);
    onViewChanged();
  }

  // Points the window at the viewport's saved position: the tail when
  // pinned, otherwise the rows around the saved anchor. The restore then
  // takes its final position from the mounted anchor.
  function aimWindow(viewport) {
    if (viewport.pinned) {
      layout.window.showTail();
    } else if (viewport.anchorId) {
      layout.window.showAround(viewport.anchorId, viewport.anchorDelta);
    } else {
      layout.window.showOffset(viewport.fallbackTop);
    }
    correctedLayoutKey = null;
    if (virtualActive) {
      publish();
    }
  }

  function saveViewport(sessionId) {
    if (!sessionId) {
      return;
    }
    const viewport = viewportFor(sessionId);
    if (isNearBottom()) {
      viewport.pinned = true;
      viewport.anchorId = '';
      return;
    }
    viewport.pinned = false;
    captureAnchor(viewport);
  }

  function applyRestore() {
    restorePending = false;
    const viewport = viewportFor(currentSessionId);
    if (restoreToPinned) {
      viewport.pinned = true;
      writeBottom();
      onViewChanged();
      return;
    }
    const anchorTop = findAnchorTop(viewport);
    if (anchorTop !== null) {
      writeScroll(anchorTop);
      onViewChanged();
      return;
    }
    if (
      viewport.fallbackScrollHeight > 0 &&
      container.scrollHeight === viewport.fallbackScrollHeight
    ) {
      // Same content height since capture: the absolute pixel position is
      // still safe (layout-less environments, stable content).
      writeScroll(viewport.fallbackTop);
      onViewChanged();
      return;
    }
    // The anchor is gone and the height changed: the bottom is the only safe,
    // non-surprising landing. Resume following from there.
    viewport.pinned = true;
    if (virtualActive) {
      aimWindow(viewport);
    }
    writeBottom();
    onViewChanged();
  }

  // Explicit follow requests: jump-to-latest click, submitted turn, sub-agent
  // live tail.
  function pinToBottom() {
    restorePending = false;
    const viewport = viewportFor(currentSessionId);
    viewport.pinned = true;
    viewport.anchorId = '';
    aimWindow(viewport);
    writeBottom();
    onViewChanged();
  }

  // A session opened explicitly (sub-agent link) starts pinned at the bottom
  // even though its saved viewport was higher.
  function forceFollowOnNextRestore() {
    const viewport = viewportFor(currentSessionId);
    viewport.pinned = true;
    viewport.anchorId = '';
    restorePending = true;
    restoreToPinned = true;
    aimWindow(viewport);
  }

  // Real user input releases the follow pin before the browser has scrolled,
  // so concurrent content growth cannot yank the view back down — and it
  // cancels a still-pending passive restore, because a user reaching for the
  // viewport wins over it.
  function noteUserInput({ upward = false } = {}) {
    if (!upward || destroyed) {
      return;
    }
    // At the top an upward gesture cannot move the viewport, so no scroll
    // event will arrive to settle a released follow pin. A short timeline
    // stays pinned; an overflowing one already has a reading position,
    // even if its initial restore has not run yet.
    if (container.scrollTop <= 0) {
      if (!isNearBottom()) {
        restorePending = false;
        classifyUserScroll(container.scrollTop);
      } else {
        maybeRequestLoadOlder();
      }
      return;
    }
    restorePending = false;
    pendingUserScrollTop ??= container.scrollTop;
    const viewport = viewportFor(currentSessionId);
    if (!viewport.pinned) {
      maybeRequestLoadOlder();
      return;
    }
    viewport.pinned = false;
    onViewChanged();
    maybeRequestLoadOlder();
  }

  // --- Window ---------------------------------------------------------------

  function publish() {
    if (!destroyed) {
      onWindowChanged();
    }
  }

  // Holds change from component effects; the renderer re-plans after them.
  function queuePublish() {
    if (!virtualActive || publishQueued) {
      return;
    }
    publishQueued = true;
    queueMicrotask(() => {
      publishQueued = false;
      publish();
    });
  }

  // The rows and spacers to mount for `items` of the Session `sessionId`, as
  // keyed entries: rows `{ key, item }`, spacers `{ key, spacer, height }`.
  // Before the timeline has shown layout, every row.
  function renderPlan(items, sessionId = currentSessionId) {
    const { model, window: rowWindow } = layoutFor(sessionId || '');
    model.sync(items);
    if (model.setWidth(content?.getBoundingClientRect().width)) {
      measureMountedRows(model);
      const viewport = viewportFor(sessionId);
      if (viewport.pinned) rowWindow.showTail();
      else if (viewport.anchorId)
        rowWindow.showAround(viewport.anchorId, viewport.anchorDelta);
      else rowWindow.showOffset(viewport.fallbackTop);
      correctedLayoutKey = null;
    }
    if (!virtualActive) {
      plannedLayoutKey = null;
      return stableEntries(items.map((item) => ({ key: item.id, item })));
    }
    const { entries, layoutKey } = rowWindow.plan(items);
    plannedLayoutKey = `${sessionId}\n${layoutKey}`;
    return stableEntries(entries);
  }

  function stableEntries(entries) {
    const nextEntries = new Map();
    const stable = entries.map((entry) => {
      const previous = planEntries.get(entry.key);
      const unchanged = entry.spacer
        ? previous?.spacer && previous.height === entry.height
        : previous?.item === entry.item;
      const result = unchanged ? previous : entry;
      nextEntries.set(entry.key, result);
      return result;
    });
    planEntries = nextEntries;
    return stable;
  }

  // The renderer applied the latest plan. Measures rows that just mounted,
  // corrects the position before the browser paints when mounted rows or
  // spacers changed, and extends the window if the viewport is not covered.
  // Then loads Run history gaps that came near the settled position.
  function rendered() {
    applyRenderedLayout();
    forgetRunGapLoads('loaded');
    maybeLoadRunGaps();
  }

  function applyRenderedLayout() {
    const layoutReady = hasLayout();
    observeRows(layoutReady);
    if (!virtualActive) {
      if (layoutReady && contentHasLayout()) {
        activate();
      }
      return;
    }
    if (!layoutReady) {
      return;
    }
    if (plannedLayoutKey !== correctedLayoutKey) {
      correctedLayoutKey = plannedLayoutKey;
      settlePosition();
      captureMissingAnchor();
    }
    if (syncWindowUpdates >= MAX_SYNC_WINDOW_UPDATES_PER_FRAME) {
      queueWindowSync();
      return;
    }
    if (syncWindow()) {
      if (syncWindowUpdates === 0) {
        requestFrame(() => {
          syncWindowUpdates = 0;
        });
      }
      syncWindowUpdates += 1;
    }
  }

  function contentHasLayout() {
    return Boolean(content && content.getBoundingClientRect().height > 0);
  }

  // All rows are mounted and measured at this point, so the window collapses
  // around the current position without moving it.
  function activate() {
    virtualActive = true;
    const viewport = viewportFor(currentSessionId);
    if (!restorePending && !viewport.pinned) {
      captureAnchor(viewport);
    }
    aimWindow(viewport);
  }

  // A reading position taken while only a spacer was visible has no anchor;
  // take it from the rows mounted there now.
  function captureMissingAnchor() {
    const viewport = viewportFor(currentSessionId);
    if (
      !restorePending &&
      pendingUserScrollTop === null &&
      !viewport.pinned &&
      !viewport.anchorId
    ) {
      captureAnchor(viewport);
    }
  }

  // Observes rows that mounted since the last update and measures them at
  // once, so the window decision after this update already knows them.
  function observeRows(layoutReady) {
    if (!content) {
      return;
    }
    for (const element of observedRows.keys()) {
      if (!element.isConnected) {
        resizeObserver?.unobserve(element);
        observedRows.delete(element);
      }
    }
    const { model } = layout;
    for (const element of content.children) {
      const id = element.dataset?.timelineItemId;
      if (id === undefined) {
        continue;
      }
      const observed = observedRows.get(element);
      if (observed?.id === id && observed.model === model) {
        continue;
      }
      if (!observed) {
        resizeObserver?.observe(element);
      }
      observedRows.set(element, { id, model });
      if (layoutReady) {
        model.setMeasured(id, element.getBoundingClientRect().height);
      }
    }
  }

  function handleResize(entries) {
    const layoutReady = hasLayout();
    const widthChanged =
      layoutReady &&
      layout.model.setWidth(content?.getBoundingClientRect().width);
    if (widthChanged) {
      // ResizeObserver need not include every row in this delivery. Seed the
      // new width from all mounted rows and discard every offscreen height.
      measureMountedRows(layout.model);
      // Keep the row/text anchor while spacer estimates change. The normal
      // post-render correction applies against the new layout before paint.
      aimWindow(viewportFor(currentSessionId));
    }
    for (const entry of entries) {
      const row = observedRows.get(entry.target);
      if (!row || !layoutReady) {
        continue;
      }
      row.model.setMeasured(
        row.id,
        entry.borderBoxSize?.[0]?.blockSize ??
          entry.target.getBoundingClientRect().height,
      );
    }
    if (!virtualActive && layoutReady && contentHasLayout()) {
      activate();
    }
    queueContentChanged();
  }

  function measureMountedRows(model) {
    for (const [element, row] of observedRows) {
      if (row.model === model && element.isConnected) {
        model.setMeasured(row.id, element.getBoundingClientRect().height);
      }
    }
    model.refreshEstimates();
  }

  // Content y of the top spacer, which starts the row list.
  function listOrigin() {
    for (const element of content?.children ?? []) {
      if (element.dataset?.timelineSpacer !== undefined) {
        return (
          element.getBoundingClientRect().top -
          container.getBoundingClientRect().top +
          container.scrollTop
        );
      }
    }
    return null;
  }

  // Moves the window to cover the viewport; returns whether it changed.
  function syncWindow() {
    if (!virtualActive || restorePending || !hasLayout()) {
      return false;
    }
    const origin = listOrigin();
    if (origin === null) {
      return false;
    }
    const viewport = viewportFor(currentSessionId);
    const changed = layout.window.update(
      container.scrollTop - origin,
      container.clientHeight,
      { followTail: viewport.pinned && pendingUserScrollTop === null },
    );
    if (changed) {
      publish();
    }
    return changed;
  }

  function queueWindowSync() {
    if (windowSyncFrame !== null) {
      return;
    }
    windowSyncFrame = requestFrame(() => {
      windowSyncFrame = null;
      syncWindow();
    });
  }

  // Keeps row `id` of the displayed Session mounted outside the window until
  // the returned release runs.
  function hold(id) {
    const release = layout.window.hold(id);
    queuePublish();
    return () => {
      if (release()) {
        queuePublish();
      }
    };
  }

  function destroy() {
    destroyed = true;
    container.removeEventListener('scroll', handleContainerScroll);
    if (contentSyncFrame !== null) {
      cancelFrame(contentSyncFrame);
    }
    if (windowSyncFrame !== null) {
      cancelFrame(windowSyncFrame);
    }
    contentSyncFrame = null;
    contentSyncQueued = false;
    windowSyncFrame = null;
    resizeObserver?.disconnect();
    interactionHolds?.destroy();
    observedRows.clear();
    loadingOlderSessions.clear();
    runGapLoads.clear();
    viewports.clear();
    layouts.clear();
  }

  return {
    contentChanged: queueContentChanged,
    sessionChanged,
    pinToBottom,
    forceFollowOnNextRestore,
    noteUserInput,
    isNearBottom,
    isRestorePending: () => restorePending,
    renderPlan,
    rendered,
    hold,
    destroy,
  };
}
