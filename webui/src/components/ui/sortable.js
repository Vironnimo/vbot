// Interaction policy for SortableList, the shared reorderable list. dnd-kit
// supplies pointer tracking, animated displacement and auto-scroll; this
// module decides when a drag may start and how keyboard moves are expressed,
// so every sortable surface behaves the same way.
import {
  AutoScroller,
  Cursor,
  DragDropManager,
  Feedback,
  PointerActivationConstraints,
  PointerSensor,
  PreventSelection,
} from '@dnd-kit/dom';
import { showPopover } from '@dnd-kit/dom/utilities';

// Text entry keeps its own pointer and arrow-key behavior (caret placement,
// selection). Buttons and links stay draggable: a press that moves further
// than the activation distance becomes a drag, a plain press stays a click.
const DRAG_EXEMPT_SELECTOR = [
  'input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"])',
  'textarea',
  'select',
  '[contenteditable]:not([contenteditable="false"])',
  '[data-sortable-ignore]',
].join(', ');

export function isDragExempt(target) {
  return (
    target instanceof Element && target.closest(DRAG_EXEMPT_SELECTOR) !== null
  );
}

export const SORTABLE_SENSORS = [
  PointerSensor.configure({
    // Mouse and pen start after a short movement, so a press held still is
    // always a click; touch needs a brief hold so the list can still scroll.
    activationConstraints(event) {
      if (event.pointerType === 'touch') {
        return [
          new PointerActivationConstraints.Delay({ value: 250, tolerance: 5 }),
        ];
      }
      return [new PointerActivationConstraints.Distance({ value: 4 })];
    },
    preventActivation: (event) => isDragExempt(event.target),
  }),
];

// dnd-kit's Accessibility plugin is left out on purpose: it would turn every
// row into a focusable `role="button"`, overriding the rows' own controls.
// SortableList provides the instructions, keyboard moves and announcements.
export const SORTABLE_PLUGINS = [
  AutoScroller,
  Cursor,
  Feedback,
  PreventSelection,
];

/**
 * Create the drag manager for one list. The configuration is passed at
 * construction as well as to the provider: rows register before the provider
 * applies its configuration, and a default plugin active at registration
 * leaves its attributes behind.
 */
export function createSortableManager() {
  return new DragDropManager({
    plugins: SORTABLE_PLUGINS,
    sensors: SORTABLE_SENSORS,
  });
}

/**
 * Keep the dragged row floating after the list re-rendered its order. The row
 * floats in the top layer as a popover; moving its DOM node closes the popover
 * without an event, and dnd-kit reopens it only when the node also lost its
 * placeholder neighbour.
 */
export function keepDragFeedbackVisible(element) {
  if (element?.hasAttribute('data-dnd-dragging')) {
    showPopover(element);
  }
}

/**
 * Return the target index for an Alt+ArrowUp/Alt+ArrowDown keyboard move of
 * the row at `index`, or `null` when the key event is not a move request.
 * An out-of-range target is returned as is; the caller treats it as a no-op.
 */
export function keyboardMoveTarget(event, index) {
  if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) {
    return null;
  }
  if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') {
    return null;
  }
  if (isDragExempt(event.target)) {
    return null;
  }
  return index + (event.key === 'ArrowUp' ? -1 : 1);
}

export function moveItem(list, from, to) {
  const next = [...list];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}

/**
 * Order `items` by the draft key order. Items the draft does not know (added
 * while a move is pending) keep their relative order at the end; keys whose
 * item disappeared are dropped.
 */
export function orderByKeys(items, keys, getKey) {
  const byKey = new Map(items.map((item) => [getKey(item), item]));
  const ordered = keys
    .filter((key) => byKey.has(key))
    .map((key) => byKey.get(key));
  const known = new Set(keys);
  return [...ordered, ...items.filter((item) => !known.has(getKey(item)))];
}
