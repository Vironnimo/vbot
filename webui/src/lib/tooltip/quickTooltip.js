// Quick tooltip: one shared `#app-tooltip` element for every `use:tooltip`
// anchor. See lib/tooltip.js for the content, placement and interaction
// contract.

import { clipsContent, normalizePlacement } from './geometry.js';
import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS,
  TOOLTIP_SKIP_DELAY_MS,
  addDescribedBy,
  createFloatingLayer,
  createHoverExit,
  isInside,
  isKeyboardModality,
  removeDescribedBy,
  trackInputModality,
} from './layers.js';

const TOOLTIP_ID = 'app-tooltip';
const EMPTY_CONTENT = Object.freeze({
  text: '',
  title: '',
  rows: [],
  mono: false,
  placement: 'top',
  selectable: false,
  whenTruncated: false,
  alignTo: '',
});

function normalizeText(value) {
  return value == null ? '' : String(value).trim();
}

const ROW_TONES = new Set(['success', 'warning', 'danger', 'muted']);

function normalizeRows(rows) {
  if (!Array.isArray(rows)) {
    return [];
  }
  return rows
    .map((row) => ({
      label: normalizeText(row?.label),
      value: normalizeText(row?.value),
      mono: row?.mono === true,
      tone: ROW_TONES.has(row?.tone) ? row.tone : '',
    }))
    .filter((row) => row.value);
}

/**
 * A string is a plain label; an object may carry structure and options; a
 * function returns either and is called each time the tooltip shows, so
 * relative times stay current and long lists build no content up front.
 */
export function normalizeTooltipContent(value) {
  if (typeof value === 'function') {
    return normalizeTooltipContent(value());
  }
  if (value === null || typeof value !== 'object') {
    const text = normalizeText(value);
    return text ? { ...EMPTY_CONTENT, text } : EMPTY_CONTENT;
  }
  return {
    text: normalizeText(value.text),
    title: normalizeText(value.title),
    rows: normalizeRows(value.rows),
    mono: value.mono === true,
    placement: normalizePlacement(value.placement),
    selectable: value.selectable === true,
    whenTruncated: value.whenTruncated === true,
    alignTo: typeof value.alignTo === 'string' ? value.alignTo.trim() : '',
  };
}

function isEmpty(content) {
  return !content.text && !content.title && content.rows.length === 0;
}

function contentKey(content) {
  return JSON.stringify(content);
}

function textBlock(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}

// Title, then the free text, then the label/value rows.
function renderContent(element, content) {
  const blocks = [];
  if (content.title) {
    blocks.push(textBlock('div', 'app-tooltip__title', content.title));
  }
  if (content.text) {
    blocks.push(
      textBlock(
        'div',
        content.mono
          ? 'app-tooltip__text app-tooltip__mono'
          : 'app-tooltip__text',
        content.text,
      ),
    );
  }
  if (content.rows.length > 0) {
    const list = document.createElement('dl');
    list.className = 'app-tooltip__rows';
    for (const row of content.rows) {
      const valueClass = [
        'app-tooltip__value',
        row.mono ? 'app-tooltip__mono' : '',
        row.tone ? `app-tooltip__value--${row.tone}` : '',
        row.label ? '' : 'app-tooltip__value--full',
      ]
        .filter(Boolean)
        .join(' ');
      if (row.label) {
        list.append(textBlock('dt', 'app-tooltip__label', row.label));
      }
      list.append(textBlock('dd', valueClass, row.value));
    }
    blocks.push(list);
  }
  element.replaceChildren(...blocks);
  element.classList.toggle(
    'app-tooltip--rich',
    Boolean(content.title) || content.rows.length > 0,
  );
  element.dataset.selectable = String(content.selectable);
}

let tooltipElement = null;
let activeAnchor = null;
let activeContent = EMPTY_CONTENT;
let activeContentKey = '';
let showTimer = null;
let showTimerOwner = null;
let labelCloseTimer = null;
let lastPointerLeavePosition = null;
let lastHiddenAt = null;
// The innermost anchor handles a bubbling focusin; enclosing anchors skip it.
const handledFocusEvents = new WeakSet();
// Anchor node -> its hover entry, so leaving a nested anchor (a status dot
// inside a list row) hands the tooltip back to the enclosing one.
const anchorEntries = new WeakMap();

// The node the bubble is placed against: the anchor, or the element inside it
// that `alignTo` selects (a list row's name).
function positionAnchor() {
  if (!activeAnchor || !activeContent.alignTo) {
    return activeAnchor;
  }
  return activeAnchor.querySelector(activeContent.alignTo) ?? activeAnchor;
}

const tooltipLayer = createFloatingLayer({
  kind: 'tooltip',
  anchor: () => activeAnchor,
  positionAnchor,
  element: () => tooltipElement,
  placement: () => activeContent.placement,
  onDismiss: () => hideTooltip(),
});

const selectableExit = createHoverExit({
  anchor: () => activeAnchor,
  element: () => tooltipElement,
  onClose: () => hideTooltip(),
});

function ensureTooltipElement() {
  if (!tooltipElement) {
    tooltipElement = document.createElement('div');
    tooltipElement.className = 'floating-tooltip app-tooltip';
    tooltipElement.id = TOOLTIP_ID;
    tooltipElement.setAttribute('role', 'tooltip');
    tooltipElement.dataset.floatingOpen = 'false';
    // Only selectable tooltips receive the pointer (styles/app/hints.css).
    tooltipElement.addEventListener('pointerenter', () =>
      selectableExit.cancel(),
    );
    tooltipElement.addEventListener('pointerleave', (event) => {
      if (activeContent.selectable) {
        selectableExit.leave(event, 'anchor');
      }
    });
  }
  if (!tooltipElement.isConnected) {
    document.body.appendChild(tooltipElement);
  }
  return tooltipElement;
}

// Warm-up: while one tooltip is visible, or shortly after it hid, moving to
// another anchor shows the next one without a new dwell.
function tooltipIsWarm() {
  if (!tooltipElement?.isConnected) {
    return false;
  }
  if (activeAnchor) {
    return true;
  }
  if (lastHiddenAt === null) {
    return false;
  }
  const elapsed = Date.now() - lastHiddenAt;
  return elapsed >= 0 && elapsed < TOOLTIP_SKIP_DELAY_MS;
}

function cancelClose() {
  if (labelCloseTimer !== null) {
    clearTimeout(labelCloseTimer);
    labelCloseTimer = null;
  }
  selectableExit.cancel();
}

// Labels let the pointer pass through, so their short linger only smooths the
// move to a neighbouring anchor; selectable tooltips close by pointer exit.
function closeOnLeave(event) {
  cancelClose();
  if (!activeAnchor) {
    return;
  }
  if (activeContent.selectable) {
    selectableExit.leave(event, 'layer');
    return;
  }
  labelCloseTimer = setTimeout(() => {
    labelCloseTimer = null;
    hideTooltip();
  }, FLOATING_HOVER_CLOSE_DELAY_MS);
}

function hideTooltip() {
  cancelPendingShow();
  cancelClose();
  if (activeAnchor) {
    removeDescribedBy(activeAnchor, TOOLTIP_ID);
    activeAnchor = null;
  }
  if (tooltipElement?.dataset.floatingOpen === 'true') {
    tooltipElement.dataset.floatingOpen = 'false';
    lastHiddenAt = Date.now();
  }
  tooltipLayer.hide();
}

// Only the instance that scheduled a pending show may cancel it: streaming
// re-renders constantly destroy unrelated tooltip anchors (remounted code-block
// copy buttons, replaced tool values), and their cleanup must never kill the
// show timer of the node the pointer is actually dwelling on.
function cancelPendingShow() {
  if (showTimer !== null) {
    clearTimeout(showTimer);
    showTimer = null;
    showTimerOwner = null;
  }
}

function clearOwnPendingShow(owner) {
  if (showTimer !== null && showTimerOwner === owner) {
    cancelPendingShow();
  }
}

function renderActiveContent(content) {
  const key = contentKey(content);
  if (key === activeContentKey) {
    return;
  }
  activeContent = content;
  activeContentKey = key;
  tooltipElement.scrollTop = 0;
  renderContent(tooltipElement, content);
  // The pointer cannot scroll a pass-through tooltip, so clipped content
  // fades out instead of showing an unreachable scrollbar.
  tooltipElement.dataset.clipped = String(
    tooltipElement.scrollHeight > tooltipElement.clientHeight + 1,
  );
}

function showTooltip(anchor, content, { instant = false } = {}) {
  cancelPendingShow();
  cancelClose();
  const element = ensureTooltipElement();
  if (activeAnchor && activeAnchor !== anchor) {
    removeDescribedBy(activeAnchor, TOOLTIP_ID);
  }
  activeAnchor = anchor;
  renderActiveContent(content);
  // A warm hand-off moves the bubble without replaying its entry motion.
  element.dataset.floatingInstant = String(instant);
  element.dataset.floatingOpen = 'true';
  addDescribedBy(anchor, TOOLTIP_ID);
  tooltipLayer.show();
}

function enclosingAnchorEntry(node, target) {
  for (
    let current = target instanceof Node ? target : null;
    current;
    current = current.parentNode
  ) {
    if (current === node) {
      return null;
    }
    const entry = anchorEntries.get(current);
    if (entry && current.contains(node)) {
      return entry;
    }
  }
  return null;
}

/**
 * Svelte action: show the shared quick tooltip on hover/focus. `content` is
 * a string label or an object (see lib/tooltip.js); empty content disables
 * it, so conditional hints can pass '' safely.
 */
export function tooltip(node, content = '') {
  let contentSource = content;
  // Function content is resolved only when the tooltip is about to show:
  // again at every hover, focus or tap.
  let currentContent =
    typeof content === 'function'
      ? EMPTY_CONTENT
      : normalizeTooltipContent(content);
  const releaseModality = trackInputModality();

  function available() {
    if (typeof contentSource === 'function') {
      currentContent = normalizeTooltipContent(contentSource);
    }
    return (
      !isEmpty(currentContent) &&
      (!currentContent.whenTruncated || clipsContent(node))
    );
  }

  function show(options) {
    showTooltip(node, currentContent, options);
  }

  // Streaming re-renders swap the hovered node for an identical replacement:
  // the browser fires leave + enter at the same pointer position without any
  // pointer movement. That re-entry is not a new hover intent, so the tooltip
  // shows immediately instead of restarting the dwell delay (which churn
  // faster than the delay would otherwise defeat forever).
  function isStationaryReentry(event) {
    return (
      lastPointerLeavePosition !== null &&
      event.clientX === lastPointerLeavePosition.x &&
      event.clientY === lastPointerLeavePosition.y
    );
  }

  function beginHover(event, { handoff = false } = {}) {
    if (!available()) {
      return;
    }
    if (activeAnchor === node) {
      cancelClose();
      return;
    }
    cancelPendingShow();
    if ((!handoff && isStationaryReentry(event)) || tooltipIsWarm()) {
      show({ instant: true });
      return;
    }
    showTimer = setTimeout(() => {
      showTimer = null;
      showTimerOwner = null;
      show();
    }, TOOLTIP_SHOW_DELAY_MS);
    showTimerOwner = node;
  }

  function handlePointerEnter(event) {
    // A held button means a drag or text selection is under way: stay quiet.
    if (event.pointerType === 'touch' || event.buttons) {
      return;
    }
    beginHover(event);
  }

  function handlePointerLeave(event) {
    if (event.pointerType === 'touch') {
      return;
    }
    clearOwnPendingShow(node);
    const enclosing = enclosingAnchorEntry(node, event.relatedTarget);
    if (enclosing && !event.buttons) {
      enclosing.beginHover(event, { handoff: true });
      if (activeAnchor !== node) {
        return;
      }
    }
    if (
      typeof event.clientX === 'number' &&
      typeof event.clientY === 'number'
    ) {
      lastPointerLeavePosition = { x: event.clientX, y: event.clientY };
    }
    if (activeAnchor === node) {
      closeOnLeave(event);
    }
  }

  // Activating the control dismisses its hint; touch has no hover, so a tap
  // toggles the tooltip instead.
  function handlePointerDown(event) {
    if (event.pointerType === 'touch') {
      if (!available()) {
        return;
      }
      if (activeAnchor === node) {
        hideTooltip();
      } else {
        show();
      }
      return;
    }
    clearOwnPendingShow(node);
    if (activeAnchor === node) {
      hideTooltip();
    }
  }

  function handleFocusIn(event) {
    if (!available() || handledFocusEvents.has(event)) {
      return;
    }
    handledFocusEvents.add(event);
    if (isKeyboardModality()) {
      show();
    }
  }

  function handleFocusOut(event) {
    if (isInside(node, event.relatedTarget)) {
      return;
    }
    clearOwnPendingShow(node);
    if (activeAnchor === node) {
      hideTooltip();
    }
  }

  anchorEntries.set(node, { beginHover });
  node.addEventListener('pointerenter', handlePointerEnter);
  node.addEventListener('pointerleave', handlePointerLeave);
  node.addEventListener('pointerdown', handlePointerDown);
  node.addEventListener('focusin', handleFocusIn);
  node.addEventListener('focusout', handleFocusOut);

  return {
    update(nextContent = '') {
      contentSource = nextContent;
      if (typeof nextContent === 'function' && activeAnchor !== node) {
        return;
      }
      currentContent = normalizeTooltipContent(nextContent);
      if (isEmpty(currentContent)) {
        clearOwnPendingShow(node);
        if (activeAnchor === node) {
          hideTooltip();
        }
      } else if (activeAnchor === node && tooltipElement) {
        renderActiveContent(currentContent);
        tooltipLayer.position();
      }
    },
    destroy() {
      clearOwnPendingShow(node);
      if (activeAnchor === node) {
        hideTooltip();
      }
      anchorEntries.delete(node);
      node.removeEventListener('pointerenter', handlePointerEnter);
      node.removeEventListener('pointerleave', handlePointerLeave);
      node.removeEventListener('pointerdown', handlePointerDown);
      node.removeEventListener('focusin', handleFocusIn);
      node.removeEventListener('focusout', handleFocusOut);
      releaseModality();
    },
  };
}
