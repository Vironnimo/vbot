// Shared floating-layer infrastructure for hover and focus help.
//
// Three layers share one registry, one positioning helper, and one visual
// language (`.floating-tooltip` / `.floating-card` in styles/app/hints.css).
// Every layer is rendered at <body>, so it escapes ancestor overflow and
// stacking contexts (same reasoning as the dropdown portal in dropdownPanel.js).
//
// - Quick tooltip: `use:tooltip={text}` (also the Button/CopyButton `tooltip`
//   prop) shows a short, non-interactive label or the full value of truncated
//   text in the one shared `#app-tooltip` element. It opens after
//   TOOLTIP_SHOW_DELAY_MS of hover, instantly while another tooltip is still
//   visible or was hidden less than TOOLTIP_SKIP_DELAY_MS ago, and instantly on
//   keyboard focus. The pointer may move onto it without closing it (WCAG
//   1.4.13); Escape dismisses it. While visible it describes its anchor through
//   `aria-describedby`. It is descriptive only: icon-only controls still need
//   their own accessible name.
// - Hover card: `use:floatingHoverCard` on the card element, whose initial
//   parent becomes the anchor. For structured or interactive content (Copy
//   actions, readiness notices with actions, previews). Opens after hover
//   intent, at once on keyboard focus or touch, stays open while the pointer
//   travels to and within it, and supports keyboard entry: Tab from the anchor
//   moves into the card's controls, Shift+Tab or Escape returns to the anchor.
// - Info popover: components/ui/InfoHint.svelte builds the pinnable "?"
//   explanation on `createFloatingLayer`, with the same delays and grace.
//
// Coordination: opening a layer closes every other open layer except the ones
// hosting its anchor (a Copy button tooltip inside a hover card) and a pinned
// info popover, which only another info popover or its own dismissal closes.
//
// Browsers do not dispatch pointer events on disabled form controls, so a
// tooltip that must show on a disabled button goes on a wrapping
// <span class="tooltip-anchor">. Focus listeners use focusin/focusout, so such
// a wrapper also reacts to keyboard focus inside it.

import { portal } from './dropdownPanel.js';

// Exported so callers and tests share named interaction timings.
export const TOOLTIP_SHOW_DELAY_MS = 300;
export const TOOLTIP_SKIP_DELAY_MS = 400;
export const HOVER_CARD_SHOW_DELAY_MS = 300;
// Dense, frequently crossed surfaces (Chat Tool values) wait longer before
// their hover card opens so incidental pointer travel stays quiet.
export const INTENTIONAL_HOVER_SHOW_DELAY_MS = 500;
export const FLOATING_HOVER_CLOSE_DELAY_MS = 150;

const ANCHOR_OFFSET = 6;
const EDGE_PADDING = 8;
const TOOLTIP_ID = 'app-tooltip';
const TABBABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]',
  '[contenteditable="true"]',
].join(', ');
const INTERACTIVE_SELECTOR = [
  'a[href]',
  'button',
  'input:not([type="hidden"])',
  'select',
  'textarea',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

/**
 * Position `element` (position: fixed, already measurable) anchored to
 * `anchor`: horizontally centered, above by default, below when there is no
 * room above, clamped to the viewport. Anchors with
 * `data-tooltip-placement="right"` (the compact Main menu rail) get a side
 * placement when it fits. The chosen side is exposed as
 * `data-floating-side` so the entry motion starts from the anchor.
 */
export function positionFloating(anchor, element) {
  const rect = anchor.getBoundingClientRect();
  const width = element.offsetWidth;
  const height = element.offsetHeight;

  if (
    anchor.dataset?.tooltipPlacement === 'right' &&
    rect.right + ANCHOR_OFFSET + width <= window.innerWidth - EDGE_PADDING
  ) {
    element.style.left = `${rect.right + ANCHOR_OFFSET}px`;
    element.style.top = `${Math.min(
      Math.max(EDGE_PADDING, rect.top + rect.height / 2 - height / 2),
      Math.max(EDGE_PADDING, window.innerHeight - height - EDGE_PADDING),
    )}px`;
    element.dataset.floatingSide = 'right';
    return;
  }

  const left = Math.min(
    Math.max(EDGE_PADDING, rect.left + rect.width / 2 - width / 2),
    Math.max(EDGE_PADDING, window.innerWidth - width - EDGE_PADDING),
  );
  const fitsAbove = rect.top - ANCHOR_OFFSET - height >= EDGE_PADDING;
  const top = fitsAbove
    ? rect.top - ANCHOR_OFFSET - height
    : Math.min(
        rect.bottom + ANCHOR_OFFSET,
        Math.max(EDGE_PADDING, window.innerHeight - height - EDGE_PADDING),
      );

  element.style.left = `${left}px`;
  element.style.top = `${top}px`;
  element.dataset.floatingSide = fitsAbove ? 'top' : 'bottom';
}

// ---------------------------------------------------------------------------
// Input modality: focus opens a layer at once only when it came from the
// keyboard (or script after keyboard use), mirroring `:focus-visible`. A
// pointer press that focuses a control must not pop its hint open.

let pointerModality = false;
let modalityUsers = 0;

function markKeyboardModality(event) {
  if (!event.metaKey && !event.ctrlKey && !event.altKey) {
    pointerModality = false;
  }
}

function markPointerModality() {
  pointerModality = true;
}

/**
 * Start (or share) input-modality tracking; returns its release function.
 * Tracking starts from keyboard modality.
 */
export function trackInputModality() {
  if (modalityUsers === 0) {
    pointerModality = false;
    window.addEventListener('keydown', markKeyboardModality, true);
    window.addEventListener('pointerdown', markPointerModality, true);
  }
  modalityUsers += 1;
  let released = false;
  return () => {
    if (released) {
      return;
    }
    released = true;
    modalityUsers -= 1;
    if (modalityUsers === 0) {
      window.removeEventListener('keydown', markKeyboardModality, true);
      window.removeEventListener('pointerdown', markPointerModality, true);
    }
  };
}

/** True unless the latest interaction was a pointer press. */
export function isKeyboardModality() {
  return !pointerModality;
}

// ---------------------------------------------------------------------------
// Coordination registry.

const openLayers = [];

function isInside(container, node) {
  return Boolean(container && node instanceof Node && container.contains(node));
}

function insideOpenLayer(node) {
  return openLayers.some((layer) => isInside(layer.element, node));
}

function registerOpenLayer(layer) {
  for (const other of [...openLayers]) {
    if (
      other === layer ||
      isInside(other.element, layer.anchor) ||
      (other.pinned && layer.kind !== 'popover')
    ) {
      continue;
    }
    other.close();
    removeOpenLayer(other);
  }
  if (!openLayers.includes(layer)) {
    openLayers.push(layer);
  }
}

function removeOpenLayer(layer) {
  const index = openLayers.indexOf(layer);
  if (index !== -1) {
    openLayers.splice(index, 1);
  }
}

function unregisterOpenLayer(layer) {
  if (!openLayers.includes(layer)) {
    return;
  }
  removeOpenLayer(layer);
  // Layers anchored inside this one (a tooltip on a card's Copy button)
  // cannot outlive it.
  for (const other of [...openLayers]) {
    if (isInside(layer.element, other.anchor)) {
      other.close();
      removeOpenLayer(other);
    }
  }
}

function anchorInViewport(anchor) {
  const rect = anchor.getBoundingClientRect();
  return (
    rect.bottom > 0 &&
    rect.top < window.innerHeight &&
    rect.right > 0 &&
    rect.left < window.innerWidth
  );
}

/**
 * Shared lifecycle of one open floating layer. `show()` positions the element
 * against its anchor and registers the layer, closing competing layers (see
 * the coordination rule above). While open, the layer follows scrolling and
 * resizing (dismissed once its anchor leaves the viewport) and is dismissed
 * by an outside press or Escape. `anchor`/`element` return the current nodes;
 * `onDismiss()` must close the owner's state and call `hide()`.
 * `onEscape(event)` defaults to `onDismiss`; an owner consumes Escape with
 * `event.preventDefault()` when the layer should absorb it (e.g. a pinned
 * popover inside a dialog).
 */
export function createFloatingLayer({
  kind,
  anchor,
  element,
  onDismiss,
  onEscape = onDismiss,
}) {
  let pinned = false;
  let following = false;
  const layer = {
    kind,
    get pinned() {
      return pinned;
    },
    get anchor() {
      return anchor();
    },
    get element() {
      return element();
    },
    close: () => onDismiss(),
  };

  function position() {
    const anchorNode = anchor();
    const elementNode = element();
    if (anchorNode && elementNode) {
      positionFloating(anchorNode, elementNode);
    }
  }

  function follow() {
    const anchorNode = anchor();
    if (!anchorNode) {
      return;
    }
    if (anchorInViewport(anchorNode)) {
      position();
    } else {
      onDismiss();
    }
  }

  function onScroll(event) {
    if (isInside(element(), event.target)) {
      return;
    }
    follow();
  }

  function onKeydown(event) {
    if (event.key === 'Escape') {
      onEscape(event);
    }
  }

  function onPointerDown(event) {
    const target = event.target;
    if (isInside(anchor(), target) || insideOpenLayer(target)) {
      return;
    }
    onDismiss();
  }

  return {
    show({ pinned: nextPinned = false } = {}) {
      pinned = nextPinned;
      if (!anchor() || !element()) {
        return;
      }
      position();
      registerOpenLayer(layer);
      if (!following) {
        following = true;
        window.addEventListener('keydown', onKeydown, true);
        window.addEventListener('scroll', onScroll, true);
        window.addEventListener('resize', follow);
        window.addEventListener('pointerdown', onPointerDown, true);
      }
    },
    position,
    hide() {
      if (following) {
        following = false;
        window.removeEventListener('keydown', onKeydown, true);
        window.removeEventListener('scroll', onScroll, true);
        window.removeEventListener('resize', follow);
        window.removeEventListener('pointerdown', onPointerDown, true);
      }
      unregisterOpenLayer(layer);
    },
  };
}

function addDescribedBy(target, id) {
  const ids = (target.getAttribute('aria-describedby') ?? '')
    .split(/\s+/)
    .filter(Boolean);
  if (!ids.includes(id)) {
    ids.push(id);
  }
  target.setAttribute('aria-describedby', ids.join(' '));
}

function removeDescribedBy(target, id) {
  const ids = (target.getAttribute('aria-describedby') ?? '')
    .split(/\s+/)
    .filter((value) => value && value !== id);
  if (ids.length > 0) {
    target.setAttribute('aria-describedby', ids.join(' '));
  } else {
    target.removeAttribute('aria-describedby');
  }
}

function tabbablesIn(root) {
  if (!root) {
    return [];
  }
  return [...root.querySelectorAll(TABBABLE_SELECTOR)].filter(
    (element) =>
      element.tabIndex >= 0 &&
      !element.closest('[inert], [hidden], [data-floating-open="false"]'),
  );
}

// ---------------------------------------------------------------------------
// Quick tooltip: one shared element for every `use:tooltip` anchor.

let tooltipElement = null;
let activeAnchor = null;
let showTimer = null;
let showTimerOwner = null;
let closeTimer = null;
let lastPointerLeavePosition = null;
let lastHiddenAt = null;
// The innermost anchor handles a bubbling focusin; enclosing anchors skip it.
const handledFocusEvents = new WeakSet();

const tooltipLayer = createFloatingLayer({
  kind: 'tooltip',
  anchor: () => activeAnchor,
  element: () => tooltipElement,
  onDismiss: () => hideTooltip(),
});

function ensureTooltipElement() {
  if (!tooltipElement) {
    tooltipElement = document.createElement('div');
    tooltipElement.className = 'floating-tooltip app-tooltip';
    tooltipElement.id = TOOLTIP_ID;
    tooltipElement.setAttribute('role', 'tooltip');
    tooltipElement.dataset.floatingOpen = 'false';
    // Hoverable content: the pointer may travel onto the tooltip.
    tooltipElement.addEventListener('pointerenter', cancelTooltipClose);
    tooltipElement.addEventListener('pointerleave', scheduleTooltipClose);
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

function cancelTooltipClose() {
  if (closeTimer !== null) {
    clearTimeout(closeTimer);
    closeTimer = null;
  }
}

function scheduleTooltipClose() {
  cancelTooltipClose();
  if (!activeAnchor) {
    return;
  }
  closeTimer = setTimeout(() => {
    closeTimer = null;
    hideTooltip();
  }, FLOATING_HOVER_CLOSE_DELAY_MS);
}

function hideTooltip() {
  cancelPendingShow();
  cancelTooltipClose();
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

function showTooltip(anchor, text) {
  cancelPendingShow();
  cancelTooltipClose();
  const element = ensureTooltipElement();
  if (activeAnchor && activeAnchor !== anchor) {
    removeDescribedBy(activeAnchor, TOOLTIP_ID);
  }
  activeAnchor = anchor;
  element.textContent = text;
  element.scrollTop = 0;
  element.dataset.floatingOpen = 'true';
  addDescribedBy(anchor, TOOLTIP_ID);
  tooltipLayer.show();
}

function normalizeText(value) {
  return value == null ? '' : String(value).trim();
}

/**
 * Svelte action: show the shared quick tooltip with `text` on hover/focus.
 * Empty/absent text disables it, so conditional hints can pass '' safely.
 */
export function tooltip(node, text = '') {
  let currentText = normalizeText(text);
  const releaseModality = trackInputModality();

  function show() {
    showTooltip(node, currentText);
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

  function handlePointerEnter(event) {
    if (event.pointerType === 'touch' || !currentText) {
      return;
    }
    if (activeAnchor === node) {
      cancelTooltipClose();
      return;
    }
    cancelPendingShow();
    if (isStationaryReentry(event) || tooltipIsWarm()) {
      show();
      return;
    }
    showTimer = setTimeout(() => {
      showTimer = null;
      showTimerOwner = null;
      show();
    }, TOOLTIP_SHOW_DELAY_MS);
    showTimerOwner = node;
  }

  function handlePointerLeave(event) {
    if (event.pointerType === 'touch') {
      return;
    }
    if (
      typeof event.clientX === 'number' &&
      typeof event.clientY === 'number'
    ) {
      lastPointerLeavePosition = { x: event.clientX, y: event.clientY };
    }
    clearOwnPendingShow(node);
    if (activeAnchor === node) {
      scheduleTooltipClose();
    }
  }

  // Activating the control dismisses its hint; touch has no hover, so a tap
  // toggles the tooltip instead.
  function handlePointerDown(event) {
    if (event.pointerType === 'touch') {
      if (!currentText) {
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
    if (!currentText || handledFocusEvents.has(event)) {
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

  node.addEventListener('pointerenter', handlePointerEnter);
  node.addEventListener('pointerleave', handlePointerLeave);
  node.addEventListener('pointerdown', handlePointerDown);
  node.addEventListener('focusin', handleFocusIn);
  node.addEventListener('focusout', handleFocusOut);

  return {
    update(nextText = '') {
      currentText = normalizeText(nextText);
      if (!currentText) {
        clearOwnPendingShow(node);
        if (activeAnchor === node) {
          hideTooltip();
        }
      } else if (activeAnchor === node && tooltipElement) {
        tooltipElement.textContent = currentText;
        tooltipLayer.position();
      }
    },
    destroy() {
      clearOwnPendingShow(node);
      if (activeAnchor === node) {
        hideTooltip();
      }
      node.removeEventListener('pointerenter', handlePointerEnter);
      node.removeEventListener('pointerleave', handlePointerLeave);
      node.removeEventListener('pointerdown', handlePointerDown);
      node.removeEventListener('focusin', handleFocusIn);
      node.removeEventListener('focusout', handleFocusOut);
      releaseModality();
    },
  };
}

// ---------------------------------------------------------------------------
// Hover card.

let hoverCardSequence = 0;

function normalizeHoverCardOptions(value) {
  const showDelayMs = value?.showDelayMs;
  return {
    accessible: value?.accessible !== false,
    touch: value?.touch !== false,
    openOnPress: value?.openOnPress === true,
    showDelayMs:
      Number.isFinite(showDelayMs) && showDelayMs >= 0
        ? showDelayMs
        : HOVER_CARD_SHOW_DELAY_MS,
  };
}

/**
 * Portal a structured hover card out of every ancestor overflow/stacking
 * context and position it against its initial parent (the anchor).
 *
 * Options:
 * - `showDelayMs` (default HOVER_CARD_SHOW_DELAY_MS): pointer hover intent.
 *   Keyboard focus and touch open at once.
 * - `accessible: false` keeps decorative previews out of the accessibility
 *   tree. Accessible cards describe the focused/hovered anchor through
 *   `aria-describedby`; descriptive cards get `role="tooltip"`, while cards
 *   with controls get no tooltip role (a tooltip must not be interactive).
 * - `touch: false` ignores taps, so a tap performs the anchor's own action
 *   (an attachment link) instead of opening a preview.
 * - `openOnPress: true` opens the card at once on a mouse press, for an
 *   anchor whose only purpose is revealing the card (the context ring);
 *   otherwise a press belongs to the anchor's own control.
 */
export function floatingHoverCard(node, options = {}) {
  const anchor = node.parentElement;
  if (!anchor) {
    return {};
  }

  let currentOptions = normalizeHoverCardOptions(options);
  let showTimer = null;
  let closeTimer = null;
  let descriptionTarget = null;
  let returnFocusTarget = null;
  let restoringFocus = false;
  let open = false;
  // Where the pointer rests: focus leaving while the pointer is still on the
  // anchor or card (e.g. a control disabling itself) must not close it.
  let anchorHovered = false;
  let cardHovered = false;
  const managedRole = !node.hasAttribute('role');
  const cardId = node.id || `floating-hover-card-${++hoverCardSequence}`;
  const portalAction = portal(node);
  const releaseModality = trackInputModality();
  const layer = createFloatingLayer({
    kind: 'card',
    anchor: () => anchor,
    element: () => node,
    onDismiss: () => hide(),
    onEscape: (event) => {
      if (isInside(node, document.activeElement)) {
        event.preventDefault();
      }
      hide();
    },
  });

  node.id = cardId;
  node.dataset.floatingHoverCard = '';
  node.dataset.floatingOpen = 'false';
  node.setAttribute('aria-hidden', 'true');

  function cancelScheduledShow() {
    if (showTimer !== null) {
      clearTimeout(showTimer);
      showTimer = null;
    }
  }

  function cancelScheduledClose() {
    if (closeTimer !== null) {
      clearTimeout(closeTimer);
      closeTimer = null;
    }
  }

  function unlinkDescription() {
    if (descriptionTarget) {
      removeDescribedBy(descriptionTarget, cardId);
      descriptionTarget = null;
    }
  }

  function linkDescription(target) {
    unlinkDescription();
    if (!open || !currentOptions.accessible || !(target instanceof Element)) {
      return;
    }
    addDescribedBy(target, cardId);
    descriptionTarget = target;
  }

  function applySemantics() {
    const exposed = open && currentOptions.accessible;
    node.setAttribute('aria-hidden', exposed ? 'false' : 'true');
    if (!managedRole) {
      return;
    }
    if (exposed && !node.querySelector(INTERACTIVE_SELECTOR)) {
      node.setAttribute('role', 'tooltip');
    } else {
      node.removeAttribute('role');
    }
  }

  function focusAnchor() {
    const target =
      returnFocusTarget?.isConnected && anchor.contains(returnFocusTarget)
        ? returnFocusTarget
        : tabbablesIn(anchor)[0];
    if (!target) {
      return false;
    }
    restoringFocus = true;
    try {
      target.focus();
    } finally {
      restoringFocus = false;
    }
    return document.activeElement === target;
  }

  // The card is portaled to the end of <body>, so leaving its last control
  // continues with whatever follows the anchor in the page.
  function focusAfterAnchor() {
    const candidates = tabbablesIn(document.body).filter(
      (element) =>
        !isInside(anchor, element) &&
        !isInside(node, element) &&
        Boolean(
          anchor.compareDocumentPosition(element) &
          Node.DOCUMENT_POSITION_FOLLOWING,
        ),
    );
    for (const candidate of candidates) {
      candidate.focus();
      if (document.activeElement === candidate) {
        return true;
      }
    }
    return false;
  }

  function show(focusTarget = null) {
    cancelScheduledShow();
    cancelScheduledClose();
    open = true;
    node.dataset.floatingOpen = 'true';
    applySemantics();
    linkDescription(focusTarget ?? anchor);
    layer.show();
  }

  function hide() {
    cancelScheduledShow();
    cancelScheduledClose();
    if (!open) {
      return;
    }
    const focusWasInside = isInside(node, document.activeElement);
    open = false;
    cardHovered = false;
    if (focusWasInside) {
      focusAnchor();
    }
    node.dataset.floatingOpen = 'false';
    applySemantics();
    unlinkDescription();
    layer.hide();
  }

  function scheduleClose() {
    cancelScheduledShow();
    cancelScheduledClose();
    if (!open) {
      return;
    }
    closeTimer = setTimeout(() => {
      closeTimer = null;
      hide();
    }, FLOATING_HOVER_CLOSE_DELAY_MS);
  }

  // Keyboard focus inside the card keeps it open while the pointer wanders.
  function schedulePointerClose(event) {
    if (
      event.pointerType === 'touch' ||
      isInside(node, document.activeElement)
    ) {
      return;
    }
    scheduleClose();
  }

  function onAnchorPointerEnter(event) {
    if (event.pointerType === 'touch') {
      return;
    }
    anchorHovered = true;
    cancelScheduledClose();
    if (open) {
      return;
    }
    cancelScheduledShow();
    if (currentOptions.showDelayMs === 0) {
      show();
      return;
    }
    showTimer = setTimeout(() => {
      showTimer = null;
      show();
    }, currentOptions.showDelayMs);
  }

  function onAnchorPointerLeave(event) {
    if (event.pointerType === 'touch') {
      return;
    }
    anchorHovered = false;
    cancelScheduledShow();
    schedulePointerClose(event);
  }

  function onCardPointerEnter(event) {
    if (event.pointerType !== 'touch') {
      cardHovered = true;
    }
    cancelScheduledClose();
  }

  function onCardPointerLeave(event) {
    if (event.pointerType !== 'touch') {
      cardHovered = false;
    }
    schedulePointerClose(event);
  }

  function onAnchorPointerDown(event) {
    if (event.pointerType !== 'touch') {
      // Pressing the anchor activates its own control; that wins over a
      // pending hover open unless revealing the card is the anchor's purpose.
      if (currentOptions.openOnPress) {
        show();
      } else {
        cancelScheduledShow();
      }
      return;
    }
    if (!currentOptions.touch) {
      cancelScheduledShow();
    } else if (open) {
      hide();
    } else {
      show();
    }
  }

  function onAnchorFocusIn(event) {
    if (restoringFocus) {
      return;
    }
    returnFocusTarget = event.target instanceof Element ? event.target : null;
    if (open) {
      cancelScheduledClose();
      linkDescription(event.target);
    } else if (isKeyboardModality()) {
      show(event.target);
    }
  }

  function onFocusOut(event) {
    if (
      anchorHovered ||
      cardHovered ||
      isInside(anchor, event.relatedTarget) ||
      isInside(node, event.relatedTarget)
    ) {
      return;
    }
    scheduleClose();
  }

  function onAnchorKeydown(event) {
    if (
      event.key !== 'Tab' ||
      event.shiftKey ||
      event.defaultPrevented ||
      !open ||
      !currentOptions.accessible
    ) {
      return;
    }
    const targets = tabbablesIn(node);
    const ownTargets = tabbablesIn(anchor);
    // Tab first walks the anchor's own controls, then enters the card.
    if (
      targets.length === 0 ||
      (ownTargets.length > 0 && ownTargets.at(-1) !== event.target)
    ) {
      return;
    }
    event.preventDefault();
    cancelScheduledClose();
    targets[0].focus();
  }

  function onCardKeydown(event) {
    if (event.key !== 'Tab' || event.defaultPrevented) {
      return;
    }
    const targets = tabbablesIn(node);
    if (targets.length === 0) {
      return;
    }
    if (event.shiftKey && event.target === targets[0]) {
      event.preventDefault();
      focusAnchor();
      cancelScheduledClose();
    } else if (!event.shiftKey && event.target === targets.at(-1)) {
      event.preventDefault();
      if (!focusAfterAnchor()) {
        focusAnchor();
      }
      hide();
    }
  }

  anchor.addEventListener('pointerenter', onAnchorPointerEnter);
  anchor.addEventListener('pointerleave', onAnchorPointerLeave);
  anchor.addEventListener('pointerdown', onAnchorPointerDown);
  anchor.addEventListener('focusin', onAnchorFocusIn);
  anchor.addEventListener('focusout', onFocusOut);
  anchor.addEventListener('keydown', onAnchorKeydown);
  node.addEventListener('pointerenter', onCardPointerEnter);
  node.addEventListener('pointerleave', onCardPointerLeave);
  node.addEventListener('focusin', cancelScheduledClose);
  node.addEventListener('focusout', onFocusOut);
  node.addEventListener('keydown', onCardKeydown);

  return {
    update(nextOptions = {}) {
      currentOptions = normalizeHoverCardOptions(nextOptions);
      applySemantics();
      if (open) {
        linkDescription(descriptionTarget ?? anchor);
      }
    },
    destroy() {
      hide();
      anchor.removeEventListener('pointerenter', onAnchorPointerEnter);
      anchor.removeEventListener('pointerleave', onAnchorPointerLeave);
      anchor.removeEventListener('pointerdown', onAnchorPointerDown);
      anchor.removeEventListener('focusin', onAnchorFocusIn);
      anchor.removeEventListener('focusout', onFocusOut);
      anchor.removeEventListener('keydown', onAnchorKeydown);
      node.removeEventListener('pointerenter', onCardPointerEnter);
      node.removeEventListener('pointerleave', onCardPointerLeave);
      node.removeEventListener('focusin', cancelScheduledClose);
      node.removeEventListener('focusout', onFocusOut);
      node.removeEventListener('keydown', onCardKeydown);
      delete node.dataset.floatingHoverCard;
      delete node.dataset.floatingOpen;
      portalAction.destroy();
      releaseModality();
    },
  };
}
