// Shared floating-layer infrastructure for hover and focus help.
//
// Three layers share one registry, one positioning helper, and one visual
// language (`.floating-tooltip` / `.floating-card` in styles/app/hints.css).
// Every layer is rendered at <body>, so it escapes ancestor overflow and
// stacking contexts (same reasoning as the dropdown portal in dropdownPanel.js).
// Internal modules under lib/tooltip/ hold geometry, the shared layer
// lifecycle, the quick tooltip and the hover card.
//
// - Quick tooltip: `use:tooltip={content}` (also the Button/CopyButton
//   `tooltip` prop and the Dropdown `triggerTooltip` prop) in the one shared
//   `#app-tooltip` element. `content` is either a string (a label or short
//   hint) or an object:
//     { text, title, rows: [{ label, value, mono }], mono,
//       placement, selectable, whenTruncated, alignTo }
//   `title` and `rows` render a compact details card (a heading and aligned
//   label/value pairs, e.g. a Session's Agent and last activity); `mono` sets
//   code-like values (paths, commands, ids) in the mono face. `placement`
//   ('top' default, 'right', 'bottom', 'left') is the preferred side; rows of
//   a vertically swept list use a side placement so the bubble never covers
//   the neighbouring rows. `alignTo` (a selector inside the anchor) places
//   the bubble against that element instead of the whole anchor, so a wide
//   list row that owns hover and focus can put its bubble beside its name.
//   `whenTruncated` shows the tooltip only while the
//   anchor clips its own text, for tooltips that merely repeat it in full.
//   It opens after TOOLTIP_SHOW_DELAY_MS of hover, instantly while another
//   tooltip is still visible or was hidden less than TOOLTIP_SKIP_DELAY_MS
//   ago, and instantly on keyboard focus; Escape dismisses it. By default the
//   pointer passes through it and it closes shortly after the pointer leaves
//   its anchor. `selectable: true` lets the pointer move onto it to select
//   and copy its text: it stays open while the pointer travels straight
//   toward it and closes as soon as the pointer heads elsewhere (see
//   `createHoverExit`). While visible it describes its anchor through
//   `aria-describedby`. It is descriptive only: icon-only controls still need
//   their own accessible name. Leaving a nested anchor (a status dot in a
//   list row) hands the tooltip back to the enclosing anchor.
// - Hover card: `use:floatingHoverCard` on the card element, whose initial
//   parent becomes the anchor. For structured or interactive content (Copy
//   actions, readiness notices with actions, previews). Opens after hover
//   intent, at once on keyboard focus or touch, stays open while the pointer
//   travels to and within it, and supports keyboard entry: Tab from the anchor
//   moves into the card's controls, Shift+Tab or Escape returns to the anchor.
//   Its `whenTruncated` option opens it only while the anchor clips its text,
//   like the quick tooltip's.
// - Info popover: components/ui/InfoHint.svelte builds the pinnable "?"
//   explanation on `createFloatingLayer`, with the same delays and pointer
//   exit.
//
// Coordination: opening a layer closes every other open layer except the ones
// hosting its anchor (a Copy button tooltip inside a hover card) and a pinned
// info popover, which only another info popover or its own dismissal closes.
// No layer opens while a pointer button is held (a drag or text selection).
//
// Browsers do not dispatch pointer events on disabled form controls, so a
// tooltip that must show on a disabled button goes on a wrapping
// <span class="tooltip-anchor">. Focus listeners use focusin/focusout, so such
// a wrapper also reacts to keyboard focus inside it.

export {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  HOVER_CARD_SHOW_DELAY_MS,
  INTENTIONAL_HOVER_SHOW_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS,
  TOOLTIP_SKIP_DELAY_MS,
  createFloatingLayer,
  createHoverExit,
  isKeyboardModality,
  trackInputModality,
} from './tooltip/layers.js';
export { tooltip } from './tooltip/quickTooltip.js';
export { floatingHoverCard } from './tooltip/hoverCard.js';
