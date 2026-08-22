// Shared hover-hint layer: the quick tooltip.
//
// `use:tooltip={text}` is the app-wide replacement for native `title`
// attributes: styled to the design system, multi-line capable, immediate, and
// identical everywhere. One shared floating element is rendered at <body> so
// it escapes every ancestor stacking/overflow context (same reasoning as the
// dropdown portal in dropdownPanel.js).
//
// The tooltip is presentational only — icon-only controls still need their
// own aria-label. While visible it is linked to the anchor via
// aria-describedby so screen readers announce it on focus.
//
// Browsers do not dispatch pointer events on disabled form controls, so a
// tooltip that must show on a disabled button (e.g. "why is this disabled")
// goes on a wrapping <span class="tooltip-anchor"> instead of the button.
//
// Rich hover cards use `use:floatingHoverCard` on the card element. The action
// takes its initial parent as the anchor, portals the card to <body>, and owns
// hover/focus/viewport cleanup. This is for structured or interactive content
// that cannot be flattened into the one-line quick tooltip.

import { portal } from './dropdownPanel.js';

// Exported so callers and tests share named interaction timings.
export const TOOLTIP_SHOW_DELAY_MS = 150;
export const INTENTIONAL_HOVER_SHOW_DELAY_MS = 500;
export const FLOATING_HOVER_CLOSE_DELAY_MS = 150;

const ANCHOR_OFFSET = 6;
const EDGE_PADDING = 8;
const TOOLTIP_ID = 'app-tooltip';

let tooltipElement = null;
let activeAnchor = null;
let showTimer = null;
let activeHoverCardHide = null;
let hoverCardSequence = 0;

/**
 * Position `element` (position: fixed, already measurable) anchored to
 * `anchor`: horizontally centered, above by default, below when there is no
 * room above, clamped to the viewport. Shared with the InfoHint popover.
 */
export function positionFloating(anchor, element) {
  const rect = anchor.getBoundingClientRect();
  const width = element.offsetWidth;
  const height = element.offsetHeight;

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
}

function ensureTooltipElement() {
  if (!tooltipElement) {
    tooltipElement = document.createElement('div');
    tooltipElement.className = 'app-tooltip';
    tooltipElement.id = TOOLTIP_ID;
    tooltipElement.setAttribute('role', 'tooltip');
  }
  if (!tooltipElement.isConnected) {
    document.body.appendChild(tooltipElement);
  }
  return tooltipElement;
}

function hideTooltip() {
  if (showTimer !== null) {
    clearTimeout(showTimer);
    showTimer = null;
  }
  if (activeAnchor) {
    activeAnchor.removeAttribute('aria-describedby');
    activeAnchor = null;
  }
  if (tooltipElement) {
    tooltipElement.classList.remove('app-tooltip--visible');
  }
  window.removeEventListener('keydown', onWindowKeydown, true);
  window.removeEventListener('scroll', onWindowScroll, true);
  document.removeEventListener('pointerdown', onDocumentPointerDown, true);
}

function onWindowKeydown(event) {
  if (event.key === 'Escape') {
    hideTooltip();
  }
}

function onDocumentPointerDown(event) {
  if (activeAnchor && !activeAnchor.contains(event.target)) {
    hideTooltip();
  }
}

// On scroll, reposition the tooltip to follow its anchor instead of blindly
// hiding. Chat content updates (streaming, new messages) fire scroll events
// from inner containers; the anchor (e.g. the context ring in the composer)
// often hasn't moved at all, so the tooltip should stay open and keep updating.
// Only hide when the anchor has actually scrolled out of the viewport.
function onWindowScroll() {
  if (!activeAnchor || !tooltipElement) {
    hideTooltip();
    return;
  }
  const rect = activeAnchor.getBoundingClientRect();
  const inViewport =
    rect.bottom > 0 &&
    rect.top < window.innerHeight &&
    rect.right > 0 &&
    rect.left < window.innerWidth;
  if (inViewport) {
    positionFloating(activeAnchor, tooltipElement);
  } else {
    hideTooltip();
  }
}

function showTooltip(anchor, text) {
  const element = ensureTooltipElement();
  element.textContent = text;
  element.classList.add('app-tooltip--visible');
  positionFloating(anchor, element);
  activeAnchor = anchor;
  anchor.setAttribute('aria-describedby', TOOLTIP_ID);
  window.addEventListener('keydown', onWindowKeydown, true);
  window.addEventListener('scroll', onWindowScroll, true);
  document.addEventListener('pointerdown', onDocumentPointerDown, true);
}

/**
 * Svelte action: show the shared quick tooltip with `text` on hover/focus.
 * Empty/absent text disables it, so conditional hints can pass '' safely.
 */
export function tooltip(node, text = '') {
  let currentText = normalize(text);

  function normalize(value) {
    return value == null ? '' : String(value).trim();
  }

  function scheduleShow() {
    if (!currentText || activeAnchor === node) {
      return;
    }
    if (showTimer !== null) {
      clearTimeout(showTimer);
    }
    showTimer = setTimeout(() => {
      showTimer = null;
      showTooltip(node, currentText);
    }, TOOLTIP_SHOW_DELAY_MS);
  }

  function cancelOrHide() {
    if (activeAnchor === node || showTimer !== null) {
      hideTooltip();
    }
  }

  function handlePointerDown(event) {
    if (event.pointerType !== 'touch') {
      cancelOrHide();
      return;
    }
    if (!currentText || activeAnchor === node) {
      hideTooltip();
      return;
    }
    if (showTimer !== null) {
      clearTimeout(showTimer);
      showTimer = null;
    }
    showTooltip(node, currentText);
  }

  node.addEventListener('pointerenter', scheduleShow);
  node.addEventListener('pointerleave', cancelOrHide);
  node.addEventListener('pointerdown', handlePointerDown);
  node.addEventListener('focus', scheduleShow);
  node.addEventListener('blur', cancelOrHide);

  return {
    update(nextText = '') {
      currentText = normalize(nextText);
      if (!currentText) {
        cancelOrHide();
      } else if (activeAnchor === node && tooltipElement) {
        tooltipElement.textContent = currentText;
        positionFloating(node, tooltipElement);
      }
    },
    destroy() {
      cancelOrHide();
      node.removeEventListener('pointerenter', scheduleShow);
      node.removeEventListener('pointerleave', cancelOrHide);
      node.removeEventListener('pointerdown', handlePointerDown);
      node.removeEventListener('focus', scheduleShow);
      node.removeEventListener('blur', cancelOrHide);
    },
  };
}

/**
 * Portal a structured hover card out of every ancestor overflow/stacking
 * context and position it against its initial parent.
 *
 * `accessible: false` keeps decorative previews out of the accessibility tree.
 * Accessible cards are linked to the hovered/focused anchor through
 * `aria-describedby` while visible.
 * `showDelayMs` requires pointer dwell while focus and touch stay immediate.
 */
export function floatingHoverCard(node, options = {}) {
  const anchor = node.parentElement;
  if (!anchor) {
    return {};
  }

  let currentOptions = normalizeFloatingHoverOptions(options);
  let showTimer = null;
  let closeTimer = null;
  let descriptionTarget = null;
  let ignorePointerFocus = false;
  let open = false;
  const cardId = node.id || `floating-hover-card-${++hoverCardSequence}`;
  const portalAction = portal(node);

  node.id = cardId;
  node.dataset.floatingHoverCard = '';
  node.dataset.floatingOpen = 'false';
  node.setAttribute('aria-hidden', 'true');

  function normalizeFloatingHoverOptions(value) {
    const showDelayMs = value?.showDelayMs;
    return {
      accessible: value?.accessible !== false,
      showDelayMs:
        Number.isFinite(showDelayMs) && showDelayMs > 0 ? showDelayMs : 0,
    };
  }

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
    if (!descriptionTarget) {
      return;
    }
    const ids = (descriptionTarget.getAttribute('aria-describedby') ?? '')
      .split(/\s+/)
      .filter((id) => id && id !== cardId);
    if (ids.length > 0) {
      descriptionTarget.setAttribute('aria-describedby', ids.join(' '));
    } else {
      descriptionTarget.removeAttribute('aria-describedby');
    }
    descriptionTarget = null;
  }

  function linkDescription(target) {
    unlinkDescription();
    if (!currentOptions.accessible || !(target instanceof Element)) {
      return;
    }
    const ids = (target.getAttribute('aria-describedby') ?? '')
      .split(/\s+/)
      .filter(Boolean);
    if (!ids.includes(cardId)) {
      ids.push(cardId);
    }
    target.setAttribute('aria-describedby', ids.join(' '));
    descriptionTarget = target;
  }

  function removeWindowListeners() {
    window.removeEventListener('keydown', onWindowKeydown, true);
    window.removeEventListener('resize', hide, true);
    window.removeEventListener('scroll', onWindowScroll, true);
    document.removeEventListener('pointerdown', onDocumentPointerDown, true);
  }

  function hide() {
    cancelScheduledShow();
    cancelScheduledClose();
    if (!open) {
      return;
    }
    open = false;
    node.dataset.floatingOpen = 'false';
    node.setAttribute('aria-hidden', 'true');
    unlinkDescription();
    removeWindowListeners();
    if (activeHoverCardHide === hide) {
      activeHoverCardHide = null;
    }
  }

  function show(event) {
    cancelScheduledShow();
    cancelScheduledClose();
    if (activeHoverCardHide && activeHoverCardHide !== hide) {
      activeHoverCardHide();
    }
    activeHoverCardHide = hide;
    open = true;
    node.dataset.floatingOpen = 'true';
    if (currentOptions.accessible) {
      node.setAttribute('aria-hidden', 'false');
      const eventTarget =
        event?.type === 'focusin' && event.target instanceof Element
          ? event.target
          : anchor;
      linkDescription(eventTarget);
    }
    positionFloating(anchor, node);
    window.addEventListener('keydown', onWindowKeydown, true);
    window.addEventListener('resize', hide, true);
    window.addEventListener('scroll', onWindowScroll, true);
    document.addEventListener('pointerdown', onDocumentPointerDown, true);
  }

  function scheduleClose() {
    cancelScheduledShow();
    cancelScheduledClose();
    if (!open) {
      return;
    }
    closeTimer = setTimeout(hide, FLOATING_HOVER_CLOSE_DELAY_MS);
  }

  function schedulePointerShow(event) {
    cancelScheduledShow();
    if (open || currentOptions.showDelayMs === 0) {
      show(event);
      return;
    }
    showTimer = setTimeout(() => {
      showTimer = null;
      show(event);
    }, currentOptions.showDelayMs);
  }

  function onWindowKeydown(event) {
    if (event.key === 'Escape') {
      hide();
    }
  }

  function onWindowScroll(event) {
    const target = event.target instanceof Node ? event.target : null;
    if (target && node.contains(target)) {
      return;
    }
    const rect = anchor.getBoundingClientRect();
    const inViewport =
      rect.bottom > 0 &&
      rect.top < window.innerHeight &&
      rect.right > 0 &&
      rect.left < window.innerWidth;
    if (inViewport) {
      positionFloating(anchor, node);
    } else {
      hide();
    }
  }

  function onDocumentPointerDown(event) {
    const target = event.target instanceof Node ? event.target : null;
    if (!target || anchor.contains(target) || node.contains(target)) {
      return;
    }
    hide();
  }

  function onAnchorPointerEnter(event) {
    if (event.pointerType !== 'touch') {
      schedulePointerShow(event);
    }
  }

  function onAnchorPointerDown(event) {
    if (event.pointerType !== 'touch') {
      if (currentOptions.showDelayMs > 0) {
        ignorePointerFocus = true;
        queueMicrotask(() => {
          ignorePointerFocus = false;
        });
        hide();
      }
      return;
    }
    if (open) {
      hide();
    } else {
      show(event);
    }
  }

  function onAnchorFocusIn(event) {
    if (!ignorePointerFocus) {
      show(event);
    }
  }

  anchor.addEventListener('pointerenter', onAnchorPointerEnter);
  anchor.addEventListener('pointerleave', scheduleClose);
  anchor.addEventListener('pointerdown', onAnchorPointerDown);
  anchor.addEventListener('focusin', onAnchorFocusIn);
  anchor.addEventListener('focusout', scheduleClose);
  node.addEventListener('pointerenter', cancelScheduledClose);
  node.addEventListener('pointerleave', scheduleClose);
  node.addEventListener('focusin', cancelScheduledClose);
  node.addEventListener('focusout', scheduleClose);

  return {
    update(nextOptions = {}) {
      cancelScheduledShow();
      currentOptions = normalizeFloatingHoverOptions(nextOptions);
      if (!open) {
        node.setAttribute('aria-hidden', 'true');
        unlinkDescription();
      } else if (currentOptions.accessible) {
        node.setAttribute('aria-hidden', 'false');
        linkDescription(anchor);
      } else {
        node.setAttribute('aria-hidden', 'true');
        unlinkDescription();
      }
    },
    destroy() {
      hide();
      anchor.removeEventListener('pointerenter', onAnchorPointerEnter);
      anchor.removeEventListener('pointerleave', scheduleClose);
      anchor.removeEventListener('pointerdown', onAnchorPointerDown);
      anchor.removeEventListener('focusin', onAnchorFocusIn);
      anchor.removeEventListener('focusout', scheduleClose);
      node.removeEventListener('pointerenter', cancelScheduledClose);
      node.removeEventListener('pointerleave', scheduleClose);
      node.removeEventListener('focusin', cancelScheduledClose);
      node.removeEventListener('focusout', scheduleClose);
      delete node.dataset.floatingHoverCard;
      delete node.dataset.floatingOpen;
      portalAction.destroy();
    },
  };
}
