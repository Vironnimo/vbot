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

// Exported so tests can wait out the hover delay instead of hardcoding it.
export const TOOLTIP_SHOW_DELAY_MS = 150;

const ANCHOR_OFFSET = 6;
const EDGE_PADDING = 8;
const TOOLTIP_ID = 'app-tooltip';

let tooltipElement = null;
let activeAnchor = null;
let showTimer = null;

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
  window.removeEventListener('scroll', hideTooltip, true);
}

function onWindowKeydown(event) {
  if (event.key === 'Escape') {
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
  window.addEventListener('scroll', hideTooltip, true);
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
    if (!currentText) {
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

  node.addEventListener('pointerenter', scheduleShow);
  node.addEventListener('pointerleave', cancelOrHide);
  node.addEventListener('pointerdown', cancelOrHide);
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
      node.removeEventListener('pointerdown', cancelOrHide);
      node.removeEventListener('focus', scheduleShow);
      node.removeEventListener('blur', cancelOrHide);
    },
  };
}
