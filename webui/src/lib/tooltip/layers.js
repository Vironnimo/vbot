// Shared floating-layer lifecycle: input modality, the coordination registry,
// open-layer following/dismissal, and pointer exit for hoverable layers.

import {
  normalizePlacement,
  pointInCorridor,
  pointerCorridor,
  positionFloating,
} from './geometry.js';

// Exported so callers and tests share named interaction timings.
export const TOOLTIP_SHOW_DELAY_MS = 300;
export const TOOLTIP_SKIP_DELAY_MS = 400;
export const HOVER_CARD_SHOW_DELAY_MS = 300;
// Dense, frequently crossed surfaces (Chat Tool values) wait longer before
// their hover card opens so incidental pointer travel stays quiet.
export const INTENTIONAL_HOVER_SHOW_DELAY_MS = 500;
export const FLOATING_HOVER_CLOSE_DELAY_MS = 150;

const TABBABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]',
  '[contenteditable="true"]',
].join(', ');

export function isInside(container, node) {
  return Boolean(container && node instanceof Node && container.contains(node));
}

export function tabbablesIn(root) {
  if (!root) {
    return [];
  }
  return [...root.querySelectorAll(TABBABLE_SELECTOR)].filter(
    (element) =>
      element.tabIndex >= 0 &&
      !element.closest('[inert], [hidden], [data-floating-open="false"]'),
  );
}

export function addDescribedBy(target, id) {
  const ids = (target.getAttribute('aria-describedby') ?? '')
    .split(/\s+/)
    .filter(Boolean);
  if (!ids.includes(id)) {
    ids.push(id);
  }
  target.setAttribute('aria-describedby', ids.join(' '));
}

export function removeDescribedBy(target, id) {
  const ids = (target.getAttribute('aria-describedby') ?? '')
    .split(/\s+/)
    .filter((value) => value && value !== id);
  if (ids.length > 0) {
    target.setAttribute('aria-describedby', ids.join(' '));
  } else {
    target.removeAttribute('aria-describedby');
  }
}

/** True while the user holds a text selection inside `element`. */
export function holdsSelection(element) {
  if (!element || typeof window.getSelection !== 'function') {
    return false;
  }
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
    return false;
  }
  return isInside(element, selection.getRangeAt(0).commonAncestorContainer);
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
 * against its anchor on the side `placement()` prefers and registers the
 * layer, closing competing layers (see the coordination rule in
 * lib/tooltip.js). While open, the layer follows scrolling, viewport resizing
 * (dismissed once its anchor leaves the viewport) and changes of its own size
 * (content that grows while shown), and is dismissed by an outside press or
 * Escape. `anchor`/`element` return the current nodes; `onDismiss()` must
 * close the owner's state and call `hide()`. `onEscape(event)` defaults to
 * `onDismiss`; an owner consumes Escape with `event.preventDefault()` when the
 * layer should absorb it (e.g. a pinned popover inside a dialog).
 */
export function createFloatingLayer({
  kind,
  anchor,
  element,
  placement = () => 'top',
  onDismiss,
  onEscape = onDismiss,
}) {
  let pinned = false;
  let following = false;
  let sizeObserver = null;
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
      positionFloating(
        anchorNode,
        elementNode,
        normalizePlacement(placement()),
      );
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
        // Content can change while the layer is shown (a card gaining a row
        // during a Run); keep it anchored instead of growing over the anchor.
        if (typeof ResizeObserver === 'function') {
          sizeObserver = new ResizeObserver(() => position());
          sizeObserver.observe(element());
        }
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
        sizeObserver?.disconnect();
        sizeObserver = null;
      }
      unregisterOpenLayer(layer);
    },
  };
}

// ---------------------------------------------------------------------------
// Pointer exit for hoverable layers.

function pointerPoint(event) {
  return typeof event.clientX === 'number' && typeof event.clientY === 'number'
    ? { x: event.clientX, y: event.clientY }
    : null;
}

/**
 * Close a hoverable layer when the pointer leaves it for good. Leaving the
 * anchor opens a corridor toward the layer and leaving the layer one toward
 * the anchor (see `pointerCorridor`): the layer stays open only while the
 * pointer travels inside that corridor and closes as soon as it moves
 * elsewhere, so sweeping past an anchor never drags its layer along.
 * Leaving the document closes at once; synthetic events without pointer
 * coordinates fall back to FLOATING_HOVER_CLOSE_DELAY_MS. A text selection
 * inside the layer keeps it open, so a dragged selection survives crossing
 * the edge and can still be copied; an outside press or Escape ends it.
 *
 * `leave(event, towards)` starts the exit (`towards` is 'layer' or
 * 'anchor'); `cancel()` stops it when the pointer re-enters either region.
 */
export function createHoverExit({ anchor, element, onClose }) {
  let closeTimer = null;
  let corridor = null;

  function stopCorridor() {
    if (corridor) {
      corridor = null;
      window.removeEventListener('pointermove', onPointerMove, true);
    }
  }

  function cancel() {
    if (closeTimer !== null) {
      clearTimeout(closeTimer);
      closeTimer = null;
    }
    stopCorridor();
  }

  function close() {
    cancel();
    if (!holdsSelection(element())) {
      onClose();
    }
  }

  function onPointerMove(event) {
    const point = pointerPoint(event);
    if (point && !pointInCorridor(point, corridor)) {
      close();
    }
  }

  return {
    leave(event, towards) {
      cancel();
      const point = pointerPoint(event);
      if (!point) {
        closeTimer = setTimeout(close, FLOATING_HOVER_CLOSE_DELAY_MS);
        return;
      }
      const target = towards === 'anchor' ? anchor() : element();
      const rect = target?.getBoundingClientRect();
      if (event.relatedTarget === null || !rect) {
        close();
        return;
      }
      corridor = pointerCorridor(point, rect);
      window.addEventListener('pointermove', onPointerMove, true);
    },
    cancel,
  };
}
