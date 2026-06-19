// Shared helpers for floating dropdown panels.
//
// Dropdown panels are rendered into a portal at <body> so they escape every
// ancestor stacking context (cards, modals, scroll containers). That removes
// the recurring "menu hidden behind a sibling card" class of bug, so panels
// no longer need per-usage z-index band-aids — a single high `--z-floating`
// layer (see app.css) keeps them above everything, including modals.

const EDGE_PADDING = 8;
const OFFSET = 4;
const MIN_HEIGHT = 96;
const MAX_HEIGHT = 240;
const FLIP_THRESHOLD = 200;

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
 * the viewport. `reservedHeight` accounts for non-scrolling chrome (e.g. a
 * search header) when sizing the scroll area.
 *
 * @returns {{ placement: 'top' | 'bottom', left: number, width: number,
 *             verticalRule: string, optionsMaxHeight: number }}
 */
export function computePanelPosition(
  triggerElement,
  { reservedHeight = 0, contentHeight = null } = {},
) {
  const rect = triggerElement.getBoundingClientRect();
  const width = rect.width;

  const availableBelow =
    window.innerHeight - rect.bottom - OFFSET - EDGE_PADDING;
  const availableAbove = rect.top - OFFSET - EDGE_PADDING;
  // How tall the panel wants to be: the measured content (plus any reserved
  // chrome such as a search header), capped to MAX_HEIGHT, when the caller can
  // measure it; otherwise a fixed threshold. Flip above only when the panel
  // does not fit below AND there is more room above — so a trigger near the
  // viewport bottom opens upward instead of spilling off-screen.
  const desiredHeight =
    contentHeight != null
      ? Math.min(contentHeight + reservedHeight, MAX_HEIGHT)
      : FLIP_THRESHOLD;
  const useAbove =
    availableBelow < desiredHeight && availableAbove > availableBelow;

  const available = useAbove ? availableAbove : availableBelow;
  const optionsMaxHeight = Math.max(
    MIN_HEIGHT,
    Math.min(available - reservedHeight, MAX_HEIGHT),
  );

  const left = Math.min(
    Math.max(EDGE_PADDING, rect.left),
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
