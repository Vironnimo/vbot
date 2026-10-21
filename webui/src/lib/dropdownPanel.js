// Shared helpers for floating dropdown panels.
//
// Dropdown panels are rendered into a portal at <body> so they escape every
// ancestor stacking context (cards, modals, scroll containers). That removes
// the recurring "menu hidden behind a sibling card" class of bug, so panels
// no longer need per-usage z-index band-aids — a single high `--z-floating`
// layer (see app.css) keeps them above everything, including modals.

const EDGE_PADDING = 8;
const OFFSET = 4;
// The scroll area's default cap; `computePanelPosition`'s `maxHeight` moves it.
const MAX_HEIGHT = 240;
const FLIP_THRESHOLD = 200;
const STATUS_DOTS = new Set(['idle', 'running', 'waiting', 'unread']);

/**
 * Optional per-option decorations shared by both Dropdown variants:
 * `statusDot` ('idle' | 'running' | 'waiting' | 'unread') renders the app's
 * status dot before the label (also on the trigger for the selected option),
 * `badge` a compact count pill after it, `ariaLabel` replaces the option's
 * accessible name when the visible label alone omits that status, and
 * `tooltip` (quick-tooltip content, lib/tooltip.js, or a function building it
 * at each hover) describes the option on hover. `marker` ({ label }) renders
 * a small muted check right after the label (OptionMarker.svelte): `label` is
 * its accessible name, part of the option's own name, and its hover tooltip.
 * Option tooltips open beside the list so they never cover the neighbouring
 * options.
 */
export function optionDecorations(option) {
  const statusDot = option?.statusDot;
  const badge = option?.badge;
  const ariaLabel = option?.ariaLabel;
  const tooltip = option?.tooltip;
  return {
    statusDot: STATUS_DOTS.has(statusDot) ? statusDot : '',
    badge:
      typeof badge === 'number' || (typeof badge === 'string' && badge)
        ? String(badge)
        : '',
    ariaLabel: typeof ariaLabel === 'string' ? ariaLabel : '',
    tooltip: sideTooltip(tooltip),
    marker: optionMarker(option?.marker),
  };
}

function optionMarker(marker) {
  const label = marker?.label;
  if (typeof label !== 'string' || !label) {
    return null;
  }
  return { label, tooltip: sideTooltip(label) };
}

function sideTooltip(content) {
  if (typeof content === 'function') {
    return () => sideTooltip(content());
  }
  if (typeof content === 'string') {
    return content ? { text: content, placement: 'right' } : '';
  }
  if (content !== null && typeof content === 'object') {
    return { placement: 'right', ...content };
  }
  return '';
}

/**
 * Svelte action: relocate `node` to a portal target (defaults to document.body)
 * so it stacks at the document root instead of inside its DOM ancestors.
 */
export function portal(node, target = document.body) {
  let host = target ?? document.body;

  function place() {
    if (host && node.parentNode !== host) {
      host.appendChild(node);
    }
  }

  place();

  return {
    update(nextTarget = document.body) {
      host = nextTarget ?? document.body;
      place();
    },
    destroy() {
      if (node.parentNode) {
        node.parentNode.removeChild(node);
      }
    },
  };
}

/**
 * Compute fixed-position coordinates for a panel anchored to `triggerElement`.
 * Flips above the trigger when there is little room below, and clamps within
 * the viewport: the scroll area never grows past the room on the chosen side,
 * so the whole panel always stays on screen. `reservedHeight` accounts for
 * the panel's non-scrolling chrome (a search header, a footer action, its
 * border) when sizing the scroll area. Callers may supply a wider
 * `panelWidth` (or a `minWidth` floor for the trigger width) and align its end
 * edge to a compact trigger. `maxHeight` replaces the default cap of the
 * scroll area, for a picker that serves as a page's navigation; the viewport
 * still limits it.
 *
 * @returns {{ placement: 'top' | 'bottom', left: number, width: number,
 *             verticalRule: string, optionsMaxHeight: number }}
 */
export function computePanelPosition(
  triggerElement,
  {
    reservedHeight = 0,
    contentHeight = null,
    panelWidth = null,
    minWidth = 0,
    horizontalAlign = 'start',
    maxHeight = MAX_HEIGHT,
  } = {},
) {
  const rect = triggerElement.getBoundingClientRect();
  const viewportWidth = Math.max(0, window.innerWidth - EDGE_PADDING * 2);
  const width = Math.min(
    Math.max(panelWidth ?? rect.width, minWidth),
    viewportWidth,
  );

  const availableBelow =
    window.innerHeight - rect.bottom - OFFSET - EDGE_PADDING;
  const availableAbove = rect.top - OFFSET - EDGE_PADDING;
  // How tall the panel wants to be: the measured content capped to
  // `maxHeight`, plus any reserved chrome, when the caller can measure it;
  // otherwise a fixed threshold. Flip above only when the panel does not fit
  // below AND there is more room above — so a trigger near the viewport bottom
  // opens upward instead of spilling off-screen.
  const desiredHeight =
    contentHeight != null
      ? Math.min(contentHeight, maxHeight) + reservedHeight
      : FLIP_THRESHOLD;
  const useAbove =
    availableBelow < desiredHeight && availableAbove > availableBelow;

  const available = useAbove ? availableAbove : availableBelow;
  const optionsMaxHeight = Math.max(
    0,
    Math.min(available - reservedHeight, maxHeight),
  );

  const triggerRight = rect.right ?? rect.left + rect.width;
  const preferredLeft =
    horizontalAlign === 'end' ? triggerRight - width : rect.left;
  const left = Math.min(
    Math.max(EDGE_PADDING, preferredLeft),
    Math.max(EDGE_PADDING, window.innerWidth - width - EDGE_PADDING),
  );

  const verticalRule = useAbove
    ? `bottom: ${window.innerHeight - rect.top + OFFSET}px`
    : `top: ${Math.min(window.innerHeight - EDGE_PADDING, rect.bottom + OFFSET)}px`;

  return {
    placement: useAbove ? 'top' : 'bottom',
    left,
    width,
    verticalRule,
    optionsMaxHeight,
  };
}
