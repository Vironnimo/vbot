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

const STICK_TO_BOTTOM_THRESHOLD_PX = 56;
const LOAD_OLDER_THRESHOLD_PX = 48;
const PROGRAMMATIC_ECHO_TOLERANCE_PX = 1;
const MAX_TRACKED_SESSIONS = 100;

export function createChatScrollController(
  container,
  {
    onViewChanged = () => {},
    shouldLoadOlder = () => false,
    requestLoadOlder = async () => false,
  } = {},
) {
  // sessionId -> { pinned, anchorId, anchorDelta, fallbackTop,
  //                fallbackScrollHeight }
  const viewports = new Map();
  let currentSessionId = '';
  let restorePending = false;
  let restoreToPinned = true;
  let expectedScrollTop = 0;
  // Set by upward user input and consumed by the next scroll event: the
  // gesture owns that event even when the position happens to match our last
  // write (e.g. scrolling at the very top, where nothing moves).
  let pendingUpwardInput = false;
  // True after upward input released the pin but the user has not actually
  // moved yet: there is no reading position to protect, so content growth
  // neither follows nor corrects until the first real scroll event.
  let awaitingUserPosition = false;
  let loadOlderInFlight = false;
  let contentSyncQueued = false;
  let contentSyncFrame = null;

  container.addEventListener('scroll', handleContainerScroll);

  function createViewport() {
    return {
      pinned: true,
      anchorId: '',
      anchorDelta: 0,
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
    awaitingUserPosition = false;
    pendingUpwardInput = false;
  }

  function writeBottom() {
    // Only snap when content actually fills the viewport; otherwise
    // scrollTop = scrollHeight collapses to the top during initial loads.
    if (container.scrollHeight > container.offsetHeight) {
      writeScroll(container.scrollHeight);
    }
  }

  function handleContainerScroll() {
    if (restorePending) {
      // Transition noise (clamps, programmatic resets) while a restore has
      // not been applied yet; the restore decides the position.
      expectedScrollTop = container.scrollTop;
      return;
    }
    const actual = container.scrollTop;
    const upwardGesture = pendingUpwardInput;
    pendingUpwardInput = false;
    if (
      !upwardGesture &&
      Math.abs(actual - expectedScrollTop) <= PROGRAMMATIC_ECHO_TOLERANCE_PX
    ) {
      return;
    }
    classifyUserScroll(actual);
  }

  function classifyUserScroll(actual) {
    expectedScrollTop = actual;
    awaitingUserPosition = false;
    const viewport = viewportFor(currentSessionId);
    if (isNearBottom()) {
      viewport.pinned = true;
      viewport.anchorId = '';
    } else {
      viewport.pinned = false;
      captureAnchor(viewport);
      maybeRequestLoadOlder();
    }
    onViewChanged();
  }

  function maybeRequestLoadOlder() {
    if (
      loadOlderInFlight ||
      container.scrollTop > LOAD_OLDER_THRESHOLD_PX ||
      !shouldLoadOlder()
    ) {
      return;
    }
    const requestedSessionId = currentSessionId;
    const previousScrollHeight = container.scrollHeight;
    loadOlderInFlight = true;
    Promise.resolve(requestLoadOlder())
      .catch(() => {})
      .finally(() => {
        loadOlderInFlight = false;
        if (currentSessionId !== requestedSessionId) {
          return;
        }
        // Prepended history grew the content above the reading position.
        // The anchor correction handles this when an anchor exists; without
        // one (layout-less or replaced content), shift the held pixel
        // position by the exact growth so the reading position survives.
        const growth = container.scrollHeight - previousScrollHeight;
        if (growth > 0) {
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
  function captureAnchor(viewport) {
    viewport.fallbackTop = container.scrollTop;
    viewport.fallbackScrollHeight = container.scrollHeight;
    const containerTop = container.getBoundingClientRect().top;
    const anchor = timelineItemElements().find(
      (element) =>
        elementHasLayout(element) &&
        element.getBoundingClientRect().bottom > containerTop,
    );
    if (!anchor) {
      viewport.anchorId = '';
      viewport.anchorDelta = 0;
      return;
    }
    viewport.anchorId = anchor.dataset.timelineItemId ?? '';
    viewport.anchorDelta = anchor.offsetTop - container.scrollTop;
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
    return Math.max(0, anchor.offsetTop - viewport.anchorDelta);
  }

  // Called (coalesced) after any content growth: streaming deltas, image
  // loads, history pages, session swaps.
  function contentChanged() {
    if (restorePending) {
      applyRestore();
      return;
    }
    const viewport = viewportFor(currentSessionId);
    if (awaitingUserPosition) {
      // Upward input released the pin but the user has not moved yet; if the
      // release turned out to be a no-op at the bottom, resume following.
      if (isNearBottom()) {
        viewport.pinned = true;
        awaitingUserPosition = false;
        onViewChanged();
      }
      return;
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
    const viewport = viewportFor(currentSessionId);
    restorePending = true;
    restoreToPinned = viewport.pinned;
    awaitingUserPosition = false;
    onViewChanged();
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
  }

  // Real user input releases the follow pin before the browser has scrolled,
  // so concurrent content growth cannot yank the view back down — and it
  // cancels a still-pending passive restore, because a user reaching for the
  // viewport wins over it.
  function noteUserInput({ upward = false } = {}) {
    if (!upward) {
      return;
    }
    pendingUpwardInput = true;
    restorePending = false;
    const viewport = viewportFor(currentSessionId);
    if (!viewport.pinned) {
      return;
    }
    viewport.pinned = false;
    awaitingUserPosition = true;
    onViewChanged();
    maybeRequestLoadOlder();
  }

  function destroy() {
    container.removeEventListener('scroll', handleContainerScroll);
    if (
      contentSyncFrame !== null &&
      typeof cancelAnimationFrame === 'function'
    ) {
      cancelAnimationFrame(contentSyncFrame);
    }
    contentSyncFrame = null;
    contentSyncQueued = false;
    viewports.clear();
  }

  return {
    contentChanged: queueContentChanged,
    sessionChanged,
    pinToBottom,
    forceFollowOnNextRestore,
    noteUserInput,
    isNearBottom,
    isRestorePending: () => restorePending,
    destroy,
  };
}
