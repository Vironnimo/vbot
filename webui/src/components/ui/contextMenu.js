// Caller helpers for the shared `ContextMenu.svelte` primitive.
//
// A row that offers a context menu handles two entry points and builds the
// same `menu` value from both:
//
//   oncontextmenu={(event) => {
//     event.preventDefault();
//     menu = { ...contextMenuAnchor(event), label, items };
//   }}
//   onkeydown={(event) => {
//     if (!isContextMenuKey(event)) return;
//     event.preventDefault();
//     menu = { ...contextMenuAnchor(event), label, items };
//   }}
//
// Calling `preventDefault()` also tells the Desktop context menu in AppShell
// that a component already handled the event.

const KEYBOARD_ANCHOR_FALLBACK = 8;

/**
 * True for the keys that open a context menu from the keyboard: the
 * ContextMenu (application) key and Shift+F10.
 */
export function isContextMenuKey(event) {
  if (!event) return false;
  return event.key === 'ContextMenu' || (event.key === 'F10' && event.shiftKey);
}

function isFocusTarget(node) {
  return (
    node instanceof HTMLElement &&
    node !== document.body &&
    node !== document.documentElement
  );
}

/**
 * Anchor for a menu opened by `event`: `{ x, y, returnFocus }` in viewport
 * coordinates. A pointer event opens at the pointer; a keyboard event (or a
 * keyboard-generated `contextmenu` without coordinates) opens below the
 * event target's left edge. `returnFocus` is the element that held focus
 * when the menu opened, falling back to the event's element, so closing the
 * menu returns the user where they were.
 */
export function contextMenuAnchor(event) {
  const target = event?.target instanceof Element ? event.target : null;
  const pointer =
    event?.type !== 'keydown' &&
    Number.isFinite(event?.clientX) &&
    Number.isFinite(event?.clientY) &&
    (event.clientX !== 0 || event.clientY !== 0);

  let x;
  let y;
  if (pointer) {
    x = event.clientX;
    y = event.clientY;
  } else {
    const rect = target?.getBoundingClientRect?.();
    x = rect?.left ?? KEYBOARD_ANCHOR_FALLBACK;
    y = rect?.bottom ?? KEYBOARD_ANCHOR_FALLBACK;
  }

  const active = document.activeElement;
  const current = event?.currentTarget;
  const returnFocus = isFocusTarget(active)
    ? active
    : isFocusTarget(current)
      ? current
      : isFocusTarget(target)
        ? target
        : null;

  return { x, y, returnFocus };
}
