// Keeps the floating toast stack off areas it must never cover, such as the
// Composer. The stack rises above any registered area it would overlap; an
// area hidden with its view has no size and does not count.

const CLEARANCE_GAP_PX = 8;

const areas = new Set();
const watchers = new Set();

function requiredBottom(stack) {
  const { left, right } = stack.getBoundingClientRect();
  let bottom = 0;
  for (const area of areas) {
    const rect = area.getBoundingClientRect();
    if (!rect.width || !rect.height) continue;
    if (rect.right <= left || rect.left >= right) continue;
    bottom = Math.max(bottom, window.innerHeight - rect.top + CLEARANCE_GAP_PX);
  }
  return bottom;
}

// Svelte action for an area the stack must stay clear of.
export function keepClearOfToasts(element) {
  areas.add(element);
  for (const watcher of watchers) watcher.add(element);
  return {
    destroy() {
      areas.delete(element);
      for (const watcher of watchers) watcher.remove(element);
    },
  };
}

// Reports the `bottom` offset the stack needs, 0 for its usual place. The
// parent is observed too: a sidebar toggle can move an area without resizing
// it. Returns the function that stops watching.
export function watchToastClearance(stack, onChange) {
  if (typeof ResizeObserver !== 'function') return undefined;
  const measure = () => onChange(requiredBottom(stack));
  const observer = new ResizeObserver(measure);
  const observeArea = (element) => {
    observer.observe(element);
    if (element.parentElement) observer.observe(element.parentElement);
  };
  const watcher = {
    add: observeArea,
    remove(element) {
      observer.unobserve(element);
      measure();
    },
  };
  observer.observe(stack);
  areas.forEach(observeArea);
  watchers.add(watcher);
  window.addEventListener('resize', measure);
  return () => {
    window.removeEventListener('resize', measure);
    watchers.delete(watcher);
    observer.disconnect();
  };
}
