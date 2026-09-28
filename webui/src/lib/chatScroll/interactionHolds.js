// Keeps timeline rows with ongoing user interaction mounted while they are
// outside the window, internal to the scroll controller: the row containing
// keyboard focus, so focus is not lost to an unmount, and the mounted rows the
// text selection touches, so a selection survives scrolling away.

const ROW_SELECTOR = '[data-timeline-item-id]';

// `hold(id)` keeps a row mounted and returns its release.
export function trackInteractionHolds(container, content, hold) {
  let focusRowId = '';
  let releaseFocus = null;
  // row id -> release
  const selectionHolds = new Map();

  function rowIdOf(node) {
    const element = node instanceof Element ? node : node?.parentElement;
    if (!element || !content.contains(element)) {
      return '';
    }
    return element.closest(ROW_SELECTOR)?.dataset.timelineItemId ?? '';
  }

  function holdFocus(id) {
    if (id === focusRowId) {
      return;
    }
    releaseFocus?.();
    focusRowId = id;
    releaseFocus = id ? hold(id) : null;
  }

  function selectedRowIds() {
    const selection = document.getSelection?.();
    const ids = new Set();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
      return ids;
    }
    for (const element of content.children) {
      const id = element.dataset?.timelineItemId;
      if (id !== undefined && selection.containsNode(element, true)) {
        ids.add(id);
      }
    }
    return ids;
  }

  function handleSelectionChange() {
    const ids = selectedRowIds();
    for (const [id, release] of selectionHolds) {
      if (!ids.has(id)) {
        release();
        selectionHolds.delete(id);
      }
    }
    for (const id of ids) {
      if (!selectionHolds.has(id)) {
        selectionHolds.set(id, hold(id));
      }
    }
  }

  function handleFocusIn(event) {
    holdFocus(rowIdOf(event.target));
  }

  function handleFocusOut(event) {
    holdFocus(rowIdOf(event.relatedTarget));
  }

  function releaseAll() {
    holdFocus('');
    for (const release of selectionHolds.values()) {
      release();
    }
    selectionHolds.clear();
  }

  container.addEventListener('focusin', handleFocusIn);
  container.addEventListener('focusout', handleFocusOut);
  document.addEventListener('selectionchange', handleSelectionChange);

  return {
    // The displayed rows were replaced (Session switch): nothing held so far
    // belongs to them.
    releaseAll,
    destroy() {
      container.removeEventListener('focusin', handleFocusIn);
      container.removeEventListener('focusout', handleFocusOut);
      document.removeEventListener('selectionchange', handleSelectionChange);
      releaseAll();
    },
  };
}
