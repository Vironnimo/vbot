// Hover card: a structured or interactive floating card anchored to its
// initial parent. See lib/tooltip.js for the interaction contract.

import { portal } from '../dropdownPanel.js';
import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  HOVER_CARD_SHOW_DELAY_MS,
  addDescribedBy,
  createFloatingLayer,
  createHoverExit,
  isInside,
  isKeyboardModality,
  removeDescribedBy,
  tabbablesIn,
  trackInputModality,
} from './layers.js';
import { clipsContent, normalizePlacement } from './geometry.js';

const INTERACTIVE_SELECTOR = [
  'a[href]',
  'button',
  'input:not([type="hidden"])',
  'select',
  'textarea',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

let hoverCardSequence = 0;

function normalizeHoverCardOptions(value) {
  const showDelayMs = value?.showDelayMs;
  return {
    accessible: value?.accessible !== false,
    touch: value?.touch !== false,
    openOnPress: value?.openOnPress === true,
    whenTruncated: value?.whenTruncated === true,
    placement: normalizePlacement(value?.placement),
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
 * - `placement` (default 'top'): preferred side, flipped when it does not fit.
 * - `accessible: false` keeps decorative previews out of the accessibility
 *   tree. Accessible cards describe the focused/hovered anchor through
 *   `aria-describedby`; descriptive cards get `role="tooltip"`, while cards
 *   with controls get no tooltip role (a tooltip must not be interactive).
 * - `touch: false` ignores taps, so a tap performs the anchor's own action
 *   (an attachment link) instead of opening a preview.
 * - `openOnPress: true` opens the card at once on a mouse press, for an
 *   anchor whose only purpose is revealing the card (the context ring);
 *   otherwise a press belongs to the anchor's own control.
 * - `whenTruncated: true` opens the card only while its anchor clips its
 *   text, for a card that completes a value the anchor shows in full
 *   whenever there is room (a Tool argument with a Copy action).
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
  // The card control that last held focus, and an observer noticing it
  // becoming unfocusable while the card is open (see recoverLostFocus).
  let cardFocusTarget = null;
  let contentObserver = null;
  const managedRole = !node.hasAttribute('role');
  const cardId = node.id || `floating-hover-card-${++hoverCardSequence}`;
  const portalAction = portal(node);
  const releaseModality = trackInputModality();
  const layer = createFloatingLayer({
    kind: 'card',
    anchor: () => anchor,
    element: () => node,
    placement: () => currentOptions.placement,
    onDismiss: () => hide(),
    onEscape: (event) => {
      if (isInside(node, document.activeElement)) {
        event.preventDefault();
      }
      hide();
    },
  });
  const pointerExit = createHoverExit({
    anchor: () => anchor,
    element: () => node,
    onClose: () => hide(),
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
    pointerExit.cancel();
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

  function unfocusable(element) {
    return (
      !element.isConnected ||
      element.disabled === true ||
      Boolean(element.closest('[hidden], [inert]'))
    );
  }

  // A focused card control that becomes disabled or disappears (Compact now
  // disabling itself after activation) drops keyboard focus to <body>, which
  // leaves the user nowhere near the card. Return focus to the anchor and keep
  // the card open so the updated state stays visible; Escape or Tab continue
  // from there. Focus a user moved elsewhere is left alone.
  function recoverLostFocus() {
    const lost = cardFocusTarget;
    if (!open || !lost) {
      return;
    }
    const active = document.activeElement;
    const focusDropped =
      active === null || active === document.body || active === lost;
    if (!focusDropped || !unfocusable(lost)) {
      if (active !== lost) {
        cardFocusTarget = null;
      }
      return;
    }
    cardFocusTarget = null;
    cancelScheduledClose();
    focusAnchor();
  }

  function observeContent() {
    if (contentObserver || typeof MutationObserver !== 'function') {
      return;
    }
    contentObserver = new MutationObserver(recoverLostFocus);
    contentObserver.observe(node, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['disabled', 'hidden', 'inert'],
    });
  }

  function onCardFocusIn(event) {
    cancelScheduledClose();
    cardFocusTarget = event.target instanceof Element ? event.target : null;
  }

  function show(focusTarget = null) {
    cancelScheduledShow();
    cancelScheduledClose();
    if (!open && currentOptions.whenTruncated && !clipsContent(anchor)) {
      return;
    }
    open = true;
    node.dataset.floatingOpen = 'true';
    applySemantics();
    linkDescription(focusTarget ?? anchor);
    layer.show();
    observeContent();
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
    cardFocusTarget = null;
    contentObserver?.disconnect();
    contentObserver = null;
    if (focusWasInside) {
      focusAnchor();
    }
    node.dataset.floatingOpen = 'false';
    applySemantics();
    unlinkDescription();
    layer.hide();
  }

  // Keyboard focus that left for somewhere else closes after a short grace.
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
  function closeOnPointerExit(event, towards) {
    cancelScheduledShow();
    if (
      !open ||
      event.pointerType === 'touch' ||
      isInside(node, document.activeElement)
    ) {
      return;
    }
    pointerExit.leave(event, towards);
  }

  function onAnchorPointerEnter(event) {
    if (event.pointerType === 'touch' || event.buttons) {
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
    closeOnPointerExit(event, 'layer');
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
    closeOnPointerExit(event, 'anchor');
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
    if (cardFocusTarget && event.target === cardFocusTarget) {
      if (event.relatedTarget === null) {
        // Focus left for nowhere: settle whether the control dropped it.
        queueMicrotask(recoverLostFocus);
      } else {
        cardFocusTarget = null;
      }
    }
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
  node.addEventListener('focusin', onCardFocusIn);
  node.addEventListener('focusout', onFocusOut);
  node.addEventListener('keydown', onCardKeydown);

  return {
    update(nextOptions = {}) {
      currentOptions = normalizeHoverCardOptions(nextOptions);
      applySemantics();
      if (open) {
        linkDescription(descriptionTarget ?? anchor);
        layer.position();
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
      node.removeEventListener('focusin', onCardFocusIn);
      node.removeEventListener('focusout', onFocusOut);
      node.removeEventListener('keydown', onCardKeydown);
      delete node.dataset.floatingHoverCard;
      delete node.dataset.floatingOpen;
      portalAction.destroy();
      releaseModality();
    },
  };
}
